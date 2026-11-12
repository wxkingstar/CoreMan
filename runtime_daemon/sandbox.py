"""个人助理轮次的沙箱：每轮的目录布局、bubblewrap 参数与启动自检。

一轮助理对话在 bubblewrap 里另起一个驱动进程（与普通员工共用的常驻驱动无关）：
- 可读写：工作目录（挂在 /work）、本轮的家目录、临时目录、CLI 配置目录、驱动的会话目录；
- 只读：系统目录、CLI 程序、本安装的发布目录、Python 解释器，以及本轮收窄过的模型登录凭证；
- 看不到：节点上其它一切，包括其他员工的工作目录、守护进程的数据目录和本机用户的家目录；
- 网络：单独的网络命名空间，没有出口；唯一的出路是沙箱里 127.0.0.1 上的转发口，
  经一个 Unix socket 接到守护进程的放行代理（见 egress.py、sandbox_bridge.py）。转发口在沙箱自己
  的网络命名空间里，本机其他用户连不到；Unix socket 都在只有本用户能进的目录里。

本模块只算参数，不碰网络，在任何平台上都能做单元测试；真正起沙箱只在装了 bubblewrap 的
Linux 上（`self_test` 证明这一点，证明不了的节点不声明能力，平台就不派助理轮次）。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 节点心跳里声明「这一种 AI 能在沙箱里跑助理轮次」的能力名。
CAPABILITY = "assistant_sandbox_v1"
# 能进沙箱的 AI。OpenCode 暂不支持（凭据与出网地址另算，见计划「待所有者确认的取舍」）。
PROVIDERS = ("claude", "codex")
# 沙箱里的固定路径：转录里记的工作目录永远是 /work，换节点还原也续得上。
INSIDE_WORK = "/work"
INSIDE = {
    "home": "/turn/home",
    "tmp": "/turn/tmp",
    "config": "/turn/config",
    "driver": "/turn/driver",
    "bin": "/turn/bin",
    "run": "/turn/run",
}
BRIDGE_PORT = 18080
PROXY_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
DRIVER_SOCKET = "d.sock"
EGRESS_SOCKET = "e.sock"
CONFIG_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
CREDENTIAL_FILE = {"claude": ".credentials.json", "codex": "auth.json"}
# 只读挂进沙箱的系统路径：缺的跳过，软链照原样建软链（合并 /usr 的发行版上 /bin 是软链）。
SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/pki",
    "/etc/ca-certificates",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/localtime",
    "/etc/hosts",
)
# 原样带进沙箱的非机密变量；模型凭据走 turn_credentials，不在这里。
PASSTHROUGH_ENV = ("LANG", "LC_ALL", "TZ", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")


@dataclass(frozen=True)
class TurnLayout:
    """一轮在主机上的目录：`root` 在数据目录里，`run` 在 socket 目录里（路径短，放得下 socket）。"""

    turn_id: str
    root: Path
    run: Path

    @classmethod
    def new(cls, turns_dir: Path, socket_dir: Path) -> TurnLayout:
        turn_id = secrets.token_hex(8)
        return cls(turn_id, turns_dir / turn_id, socket_dir / f"a{turn_id[:10]}")

    def host(self, name: str) -> Path:
        """沙箱里 `INSIDE[name]` 对应的主机目录。"""
        return self.run if name == "run" else self.root / name

    @property
    def secrets(self) -> Path:
        """本轮凭据副本所在目录：不挂进沙箱，只把其中的文件只读挂到配置目录里。"""
        return self.root / "secrets"

    @property
    def driver_socket(self) -> Path:
        return self.run / DRIVER_SOCKET

    @property
    def egress_socket(self) -> Path:
        return self.run / EGRESS_SOCKET

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    def create(self) -> None:
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        for name in (*INSIDE, "secrets"):
            if name != "run":
                (self.root / name).mkdir(mode=0o700, exist_ok=True)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.run, ignore_errors=True)


def system_mounts(paths: Sequence[str] = SYSTEM_PATHS) -> list[str]:
    """系统路径的挂载参数：软链建软链，目录与文件只读挂载，不存在的跳过。"""
    args: list[str] = []
    for path in paths:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.exists(path):
            args += ["--ro-bind", path, path]
    return args


def package_root(executable: Path) -> Path:
    """CLI 程序要只读挂进去的目录：在 node_modules 里就挂整个包，否则挂它所在的目录。

    `claude` / `codex` 常见的装法有单个可执行文件（目录里挂它的上级）与 npm 包（入口脚本旁边
    还有 vendor 里的原生程序）两种；后者向上找到含 package.json、且上级是 node_modules 的目录。
    """
    for parent in executable.parents:
        if parent.parent.name == "node_modules" or parent.parent.parent.name == "node_modules":
            if (parent / "package.json").is_file():
                return parent
    return executable.parent


def cli_mounts(provider: str, which: Callable[[str], str | None] = shutil.which) -> list[Path]:
    """CLI 本体（和 npm 包需要的 node）的真实所在目录。"""
    found = which(provider)
    if not found:
        return []
    real = Path(found).resolve()
    mounts = [package_root(real)]
    with real.open("rb") as handle:
        script = handle.read(2) == b"#!"
    if script:
        # npm 装的入口是 `#!/usr/bin/env node` 脚本：node 也要看得见。
        node = which("node")
        if node:
            mounts.append(Path(node).resolve().parent)
    return mounts


def link_cli(
    layout: TurnLayout, provider: str, which: Callable[[str], str | None] = shutil.which
) -> bool:
    """在本轮的 bin 目录里建指向 CLI 真实路径的软链（沙箱里 PATH 的第一项）；找不到 CLI 返回假。"""
    found = which(provider)
    if not found:
        return False
    link = layout.host("bin") / provider
    link.unlink(missing_ok=True)
    link.symlink_to(Path(found).resolve())
    return True


def sandbox_env(provider: str, base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """沙箱里驱动进程的全部环境变量：从空环境起步，不继承守护进程的任何变量。

    驱动会把请求里的本轮变量（能力令牌、会话坐标）叠在这一份上交给 CLI；这里只有路径、代理
    与几个行为开关，`extra` 是 turn_credentials 给的模型凭据变量（只在节点用环境变量登录时有）。
    """
    env = {
        "HOME": INSIDE["home"],
        "TMPDIR": INSIDE["tmp"],
        "PATH": f"{INSIDE['bin']}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        CONFIG_ENV[provider]: INSIDE["config"],
        "HTTPS_PROXY": PROXY_URL,
        "HTTP_PROXY": PROXY_URL,
        "https_proxy": PROXY_URL,
        "http_proxy": PROXY_URL,
        "NO_PROXY": "",
        "no_proxy": "",
    }
    if provider == "claude":
        # 不连遥测与自动更新：这些地址不在放行名单里，连了也只是失败重试。
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
    for key in PASSTHROUGH_ENV:
        if base.get(key):
            env[key] = base[key]
    env.update(extra)
    return env


@dataclass(frozen=True)
class Launch:
    """起一个沙箱需要的全部输入。"""

    bwrap: str
    layout: TurnLayout
    workdir: Path
    provider: str
    env: Mapping[str, str]
    ro_paths: Sequence[Path] = ()
    credential: Path | None = None
    system: Sequence[str] = field(default_factory=system_mounts)


def _ro_roots(launch: Launch) -> list[Path]:
    roots = [Path(p) for p in launch.ro_paths]
    system = list(launch.system)
    roots += [Path(system[i + 1]) for i, arg in enumerate(system[:-1]) if arg == "--ro-bind"]
    return roots


def _check_mounts(launch: Launch) -> None:
    """只读挂载根等于或包含轮次目录、socket 目录、工作目录时拒绝（会暴露别轮的凭证副本）。"""
    layout = launch.layout
    guarded = [p.resolve() for p in (layout.root.parent, layout.run.parent, Path(launch.workdir))]
    for root in _ro_roots(launch):
        real = root.resolve()
        if any(real == g or real in g.parents for g in guarded):
            raise ValueError("unsafe_mount")


def bwrap_argv(launch: Launch, command: Sequence[str]) -> list[str]:
    """bubblewrap 的完整参数。顺序有讲究：先建目录骨架，再挂可写目录，最后把凭据只读盖上去。

    环境变量不进参数（`/proc/<pid>/cmdline` 本机其他用户都读得到）：调用方以 `launch.env` 作为
    bwrap 进程的全部环境启动它，沙箱里的进程原样继承，守护进程自己的环境一个都不带进去。
    """
    layout = launch.layout
    _check_mounts(launch)
    args = [
        launch.bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/turn"]
    args += list(launch.system)
    for path in dict.fromkeys(str(p) for p in launch.ro_paths):
        args += ["--ro-bind", path, path]
    for name, inside in INSIDE.items():
        args += ["--bind", str(layout.host(name)), inside]
    args += ["--bind", str(launch.workdir), INSIDE_WORK]
    if launch.credential is not None:
        target = f"{INSIDE['config']}/{CREDENTIAL_FILE[launch.provider]}"
        args += ["--ro-bind", str(launch.credential), target]
    return [*args, "--chdir", INSIDE_WORK, "--", *command]


def bridge_command(python: str, bridge: Path, command: Sequence[str]) -> list[str]:
    """沙箱里的第一个进程：转发口加上被它看护的子进程（驱动或自检命令）。"""
    upstream = f"{INSIDE['run']}/{EGRESS_SOCKET}"
    return [
        python,
        "-I",
        str(bridge),
        "--port",
        str(BRIDGE_PORT),
        "--upstream",
        upstream,
        "--",
        *command,
    ]


def driver_command(binary: Path) -> list[str]:
    """沙箱里的驱动：socket 与会话目录都在本轮目录里，出网经转发口。"""
    return [
        str(binary),
        "--socket",
        f"{INSIDE['run']}/{DRIVER_SOCKET}",
        "--sessions-dir",
        INSIDE["driver"],
        "--log-file",
        "-",
        "--proxy",
        PROXY_URL,
    ]


# ---- 启动自检 ----------------------------------------------------------------


def private_dir(path: Path, uid: int | None = None) -> bool:
    """目录（软链按真实目录）属于本用户，组与其他人没有任何权限。

    个人助理的节点与普通员工的节点在同一台机器上、以不同的系统用户运行：这些目录对别的用户不可进，
    里面的转录、凭证副本与 socket 才看不到、连不上。
    """
    try:
        info = path.resolve().stat()
    except OSError:
        return False
    owner = os.getuid() if uid is None else uid
    return stat.S_ISDIR(info.st_mode) and info.st_uid == owner and info.st_mode & 0o077 == 0


# 自检在沙箱里跑的检查（参数：转发口端口、JSON {"hidden": [...], "ro": [...]}）：
# - hidden：这些路径在沙箱里不存在（主机上存在与否由调用方先核对）；
# - readonly：每个存在的只读挂载点都带只读标志，且至少核对了一个；
# - offline：网络命名空间里只有 lo，且直连外网立即以 ENETUNREACH 失败（超时、连通、
#   其他错误都算没隔离）；192.0.2.1 是文档保留地址，空命名空间里没有路由，不会发出包；
# - bridge：经转发口能连到守护进程这一侧；
# - umask：沙箱里的进程能把 umask 设成 077。
_PROBE = r"""
import errno, json, os, socket, sys
port, spec = int(sys.argv[1]), json.loads(sys.argv[2])
out = {"hidden": all(not os.path.exists(p) for p in spec["hidden"])}
mask = os.umask(0o077)
out["umask"] = mask & 0o077 == 0o077
checked = [p for p in spec["ro"] if os.path.exists(p)]
out["readonly"] = bool(checked) and all(os.statvfs(p).f_flag & os.ST_RDONLY for p in checked)
try:
    only_lo = all(name == "lo" for _, name in socket.if_nameindex())
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=3).close()
        out["offline"] = False
    except OSError as exc:
        out["offline"] = only_lo and exc.errno == errno.ENETUNREACH
except OSError:
    out["offline"] = False
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"CONNECT probe.invalid:443 HTTP/1.1\r\n\r\n")
    out["bridge"] = s.recv(64).startswith(b"HTTP/1.1 403")
    s.close()
except OSError:
    out["bridge"] = False
print(json.dumps(out))
"""


@dataclass(frozen=True)
class SelfTestResult:
    """`providers`：每种 AI 能不能在沙箱里跑；`code`：整体失败时的原因代码（不带路径与输出）。"""

    providers: dict[str, bool]
    code: str = ""


Runner = Callable[..., subprocess.CompletedProcess]


class _DenyAll:
    """自检用的放行代理替身：对任何请求都回 403，只证明转发口接得通。"""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.recv(1024)
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


@dataclass(frozen=True)
class SelfTestInputs:
    """自检要用的本机事实；守护进程按自己的配置填，测试里直接构造。"""

    turns_dir: Path
    socket_dir: Path
    release: Path
    hidden: Sequence[Path]
    clis: Mapping[str, Sequence[Path]]
    # 必须只有本用户能进的目录：家目录、数据目录、员工工作目录的上级等（本轮目录与 socket
    # 目录自动加上）。
    private: Sequence[Path] = ()
    extra_ro: Sequence[Path] = ()
    python: str = sys.executable
    system: Sequence[str] = field(default_factory=system_mounts)


def self_test(
    inputs: SelfTestInputs,
    *,
    bwrap: str | None = None,
    system_name: str | None = None,
    runner: Runner = subprocess.run,
) -> SelfTestResult:
    """按前置验证脚本的同一套检查，在本机真起几次沙箱；任何一步不过就整体不可用。

    只在 Linux 且找得到 bwrap 时才起进程。容器里没有用户命名空间、macOS、没装 bubblewrap 的
    节点在这里得到失败的结果，平台就不会把助理轮次派过来。
    """
    providers = dict.fromkeys(PROVIDERS, False)
    if (system_name or platform.system()) != "Linux":
        return SelfTestResult(providers, "unsupported_platform")
    bwrap = bwrap or shutil.which("bwrap")
    if not bwrap:
        return SelfTestResult(providers, "bwrap_missing")
    layout = TurnLayout.new(inputs.turns_dir, inputs.socket_dir)
    denier = None
    try:
        try:
            layout.create()
        except OSError:
            return SelfTestResult(providers, "layout_failed")
        if not all(private_dir(p) for p in (*inputs.private, inputs.turns_dir, inputs.socket_dir)):
            return SelfTestResult(providers, "private_dir_check_failed")
        try:
            for path in inputs.hidden:
                os.stat(path)
        except OSError:
            return SelfTestResult(providers, "hidden_check_failed")
        try:
            work = Path(tempfile.mkdtemp(dir=layout.root))
        except OSError:
            return SelfTestResult(providers, "layout_failed")
        bridge = inputs.release / "runtime_daemon" / "sandbox_bridge.py"
        ro = [inputs.release, Path(sys.base_prefix), *inputs.extra_ro]

        def run(provider: str, command: Sequence[str], extra: Sequence[Path] = ()) -> Any:
            launch = Launch(
                bwrap=bwrap,
                layout=layout,
                workdir=work,
                provider=provider,
                env=sandbox_env(provider, {}, {}),
                ro_paths=[*ro, *extra],
                system=inputs.system,
            )
            return runner(
                bwrap_argv(launch, command),
                env=dict(launch.env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )

        try:
            if run("claude", ["true"]).returncode != 0:
                return SelfTestResult(providers, "sandbox_start_failed")
            denier = _DenyAll(layout.egress_socket)
            spec = {
                "hidden": [str(p) for p in inputs.hidden],
                "ro": [str(p) for p in ro],
            }
            check = [inputs.python, "-I", "-c", _PROBE, str(BRIDGE_PORT), json.dumps(spec)]
            probe = run("claude", bridge_command(inputs.python, bridge, check))
            try:
                checks = json.loads(probe.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return SelfTestResult(providers, "probe_failed")
            for name in ("hidden", "readonly", "offline", "bridge", "umask"):
                if checks.get(name) is not True:
                    return SelfTestResult(providers, f"{name}_check_failed")
            for provider in PROVIDERS:
                mounts = inputs.clis.get(provider) or ()
                if mounts and link_cli(layout, provider):
                    result = run(provider, [provider, "--version"], mounts)
                    providers[provider] = result.returncode == 0
        except ValueError as exc:
            code = "unsafe_mount" if str(exc) == "unsafe_mount" else "probe_failed"
            return SelfTestResult(dict.fromkeys(PROVIDERS, False), code)
        except (OSError, subprocess.SubprocessError):
            return SelfTestResult(dict.fromkeys(PROVIDERS, False), "sandbox_start_failed")
        return SelfTestResult(providers, "" if any(providers.values()) else "cli_unavailable")
    finally:
        if denier is not None:
            denier.close()
        layout.remove()
