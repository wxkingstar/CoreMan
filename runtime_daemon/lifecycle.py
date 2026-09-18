"""Runtime locations, exit codes and release bundles shared by the daemon and installer.

Only the standard library is used: the installer runs this before a release is healthy.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import io
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

# sysexits EX_CONFIG: retrying cannot fix it. systemd's RestartPreventExitStatus and the
# chroot supervisor both stop on this status instead of restarting every 10 seconds.
EXIT_CONFIG = 78
SERVICE_NAME = "coreman-runtime"
LAUNCHD_LABEL = "org.coreman.runtime"
PROVIDERS = ("claude", "codex")
# sockaddr_un.sun_path is 104 bytes on macOS and 108 on Linux, both including the NUL.
SOCKET_PATH_LIMIT = 100
BUNDLE_LIMIT = 150 * 1024 * 1024
UNPACKED_LIMIT = 400 * 1024 * 1024


class FatalConfigError(RuntimeError):
    """A daemon failure that restarting the service cannot fix; an operator must act."""


class InstallError(RuntimeError):
    """An install, upgrade or uninstall step failed with an operator-facing message."""


def fatal_exit_status(service_status: str) -> int:
    # launchd cannot skip restarts per exit status; its plist restarts only unsuccessful
    # exits, so a deliberate stop has to look successful there.
    return 0 if service_status == "launchd" else EXIT_CONFIG


def home_dir() -> Path:
    # /home -> /data/home style links would otherwise break every prefix comparison.
    return Path(os.path.realpath(Path.home()))


def default_data_dir() -> Path:
    return home_dir() / ".local/share/coreman-runtime"


def manage_command_path(config_path: Path) -> Path:
    return config_path.parent / "bin/coreman-runtime"


def manage_command_script(config_path: Path, release: Path) -> str:
    """The install_service entry for operators, independent of the caller's directory.

    ``runtime_daemon`` is not installed into the venv, so ``python -m`` only finds it from
    inside the release. ``-I`` keeps the current directory and PYTHON* variables out of
    ``sys.path``; relative paths in the arguments still resolve against the caller.
    """
    loader = (
        'import sys; sys.argv[0] = "coreman-runtime"; sys.path.insert(0, sys.argv.pop(1)); '
        "from runtime_daemon.install_service import main; raise SystemExit(main())"
    )
    command = [str(release / ".venv/bin/python"), "-I", "-c", loader, str(release)]
    command += ["--config", str(config_path)]
    return (
        "#!/bin/sh\n"
        "# CoreMan Runtime 管理命令（升级、卸载、重新注册服务），运行 --help 查看用法。\n"
        "# 由安装程序生成，切换版本时自动更新，请勿手工修改。\n"
        '[ "$#" -gt 0 ] || set -- --help\n'
        "exec " + " ".join(shlex.quote(part) for part in command) + ' "$@"\n'
    )


def write_manage_command(config_path: Path, release: Path) -> Path:
    path = manage_command_path(config_path)
    script = manage_command_script(config_path, release)
    with contextlib.suppress(OSError):
        if path.read_text() == script and os.access(path, os.X_OK):
            return path
    write_private(path, script)
    path.chmod(0o700)
    return path


def write_private(path: Path, content: str | bytes) -> None:
    """Atomically replace ``path`` with a 0600 file that survives power loss."""
    data = content.encode() if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def create_private(path: Path, content: str) -> None:
    """Create ``path`` exclusively; an existing identity is never overwritten."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# Unix socket directory
# ---------------------------------------------------------------------------


def _private_runtime_dir(path: Path, uid: int) -> bool:
    if not path.is_absolute():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077


def socket_fits(directory: Path) -> bool:
    longest = max(PROVIDERS, key=len) + ".sock"
    return len(os.fsencode(str(directory / longest))) <= SOCKET_PATH_LIMIT


def socket_dir_candidates(
    data_dir: Path,
    node_id: str,
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
) -> list[Path]:
    """Preferred socket directories, best first; the last entry is the /tmp fallback."""
    system = system or platform.system()
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    suffix = node_id[:8]
    candidates = []
    if system == "Linux":
        # systemd user managers export a per-user tmpfs that tmpfiles never ages out.
        runtime = env.get("XDG_RUNTIME_DIR", "")
        if runtime and _private_runtime_dir(Path(runtime), uid):
            candidates.append(Path(runtime) / f"coreman-{suffix}")
    candidates.append(data_dir / "run")
    # Only for paths beyond the sockaddr limit (long macOS homes); the daemon's
    # discovery loop restarts a driver whose socket a tmp cleaner removed.
    candidates.append(Path("/tmp") / f"coreman-{uid}-{suffix}")
    return candidates


def choose_socket_dir(data_dir: Path, node_id: str, **kwargs) -> Path:
    candidates = socket_dir_candidates(data_dir, node_id, **kwargs)
    for candidate in candidates[:-1]:
        if socket_fits(candidate):
            return candidate
    return candidates[-1]


def ensure_private_dir(path: Path, uid: int | None = None) -> Path:
    uid = os.getuid() if uid is None else uid
    if path.is_symlink():
        raise ValueError("Socket 目录不安全")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Socket 目录不安全")
    if info.st_uid != uid:
        raise ValueError("Socket 目录属于其它用户")
    os.chmod(path, 0o700)
    return path


def remove_socket_dirs(data_dir: Path, node_id: str) -> list[Path]:
    uid = os.getuid()
    candidates = socket_dir_candidates(data_dir, node_id, system="Linux", uid=uid)
    # The service's XDG_RUNTIME_DIR may be absent from the uninstalling shell.
    candidates.append(Path(f"/run/user/{uid}") / f"coreman-{node_id[:8]}")
    removed = []
    for path in dict.fromkeys(candidates):
        with contextlib.suppress(OSError):
            if path.is_symlink() or not path.is_dir() or path.lstat().st_uid != uid:
                continue
            shutil.rmtree(path)
            removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# State file and locks
# ---------------------------------------------------------------------------


def read_state(data_dir: Path) -> dict:
    try:
        state = json.loads((data_dir / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def record_fatal(data_dir: Path, message: str) -> None:
    write_private(
        data_dir / "state.json",
        json.dumps({"online": False, "fatal": True, "error": message, "updated_at": time.time()}),
    )


def wait_online(
    data_dir: Path,
    since: float,
    timeout: float,
    *,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    """Wait for a state written after ``since``: online returns, fatal raises."""
    deadline = clock() + timeout
    while True:
        state = read_state(data_dir)
        if float(state.get("updated_at") or 0) >= since:
            if state.get("fatal"):
                raise FatalConfigError(str(state.get("error") or "Runtime 配置错误"))
            if state.get("online"):
                return
        if clock() >= deadline:
            raise TimeoutError("Runtime 未在限定时间内确认上线")
        sleep(1)


def lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def wait_for_unlock(path: Path, timeout: float, *, sleep: Callable[[float], object] = time.sleep):
    deadline = time.monotonic() + timeout
    while lock_held(path):
        if time.monotonic() >= deadline:
            raise InstallError("Runtime 进程未能在限定时间内退出：" + path.name)
        sleep(0.5)


@contextlib.contextmanager
def install_lock(data_dir: Path) -> Iterator[None]:
    with (data_dir / "install.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("另一个安装、升级或卸载操作正在进行，请稍后重试") from None
        yield


# ---------------------------------------------------------------------------
# Release bundles
# ---------------------------------------------------------------------------


def read_expected_sha256(bundle: Path, explicit: str | None = None) -> str:
    value = explicit or ""
    if not value:
        sidecar = bundle.with_name(bundle.name + ".sha256")
        try:
            value = sidecar.read_text().split()[0]
        except (OSError, IndexError):
            raise InstallError(
                "需要 --sha256，或在安装包同目录放置发布时生成的 " + sidecar.name
            ) from None
    value = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InstallError("SHA256 校验值格式无效")
    return value


def load_bundle(bundle: Path, expected: str) -> bytes:
    try:
        if bundle.stat().st_size > BUNDLE_LIMIT:
            raise InstallError("安装包超过大小上限")
        data = bundle.read_bytes()
    except OSError as exc:
        raise InstallError(f"无法读取安装包：{exc.strerror or exc}") from exc
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise InstallError("安装包 SHA256 校验失败，未做任何改动")
    return data


def extract_bundle(data: bytes, target: Path) -> None:
    base = target.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > UNPACKED_LIMIT:
            raise InstallError("安装包解压大小超过上限")
        for member in members:
            if not (base / member.name).resolve().is_relative_to(base) or not (
                member.isfile() or member.isdir()
            ):
                raise InstallError("安装包包含无效文件")
        if hasattr(tarfile, "data_filter"):
            archive.extractall(base, filter="data")
        else:  # pragma: no cover - Python builds without the tarfile filter backport
            archive.extractall(base)


def create_venv(release: Path) -> None:
    subprocess.run([sys.executable, "-m", "venv", str(release / ".venv")], check=True)
    subprocess.run(
        [
            str(release / ".venv/bin/python"),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(release / "wheels"),
            "-r",
            str(release / "runtime_daemon/requirements.txt"),
        ],
        check=True,
    )


def build_release(data: bytes, data_dir: Path, checksum: str) -> Path:
    """Unpack in a stage directory, then build the venv at its permanent location.

    A venv embeds its absolute path, so only the unpacked files are staged; the release
    directory is removed again if any later step fails.
    """
    with tempfile.TemporaryDirectory(prefix="stage-", dir=data_dir) as stage:
        unpacked = Path(stage) / "release"
        unpacked.mkdir()
        extract_bundle(data, unpacked)
        destination = data_dir / f"release-{checksum[:16]}-{secrets.token_hex(4)}"
        destination.mkdir(mode=0o700)
        try:
            shutil.copytree(unpacked, destination, dirs_exist_ok=True)
            create_venv(destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return destination


def check_release(release: Path) -> None:
    """Fail before the running service is touched if the release cannot start here."""
    try:
        subprocess.run(
            [
                str(release / ".venv/bin/python"),
                "-c",
                "import runtime_daemon.daemon, runtime_daemon.install_service",
            ],
            cwd=release,
            check=True,
            capture_output=True,
            timeout=120,
        )
        for provider in PROVIDERS:
            subprocess.run(
                [str(release / "runtime_daemon/bin" / ("runtime-" + provider)), "--version"],
                check=True,
                capture_output=True,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("新版本自检失败，安装包可能与本机平台不匹配或已损坏") from exc


def prune_releases(data_dir: Path, keep: Iterable[Path]) -> list[Path]:
    kept = {Path(p).resolve() for p in keep}
    removed = []
    for path in sorted(data_dir.glob("release-*")) + sorted(data_dir.glob("stage-*")):
        if path.is_symlink() or not path.is_dir() or path.resolve() in kept:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed
