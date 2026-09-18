"""Install, upgrade or remove the current user's service without altering an existing CLI login.

Operators run it through ``~/.local/share/coreman-runtime/bin/coreman-runtime``, which works
from any directory; ``python -m runtime_daemon.install_service`` only works inside a release.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from runtime_daemon.lifecycle import (
    EXIT_CONFIG,
    LAUNCHD_LABEL,
    SERVICE_NAME,
    FatalConfigError,
    InstallError,
    build_release,
    check_release,
    create_private,
    default_data_dir,
    install_lock,
    load_bundle,
    lock_held,
    manage_command_path,
    prune_releases,
    read_expected_sha256,
    remove_socket_dirs,
    wait_for_unlock,
    wait_online,
    write_manage_command,
    write_private,
)

# Registered and running, but not yet confirmed online: install.sh keeps the install.
EXIT_NOT_CONFIRMED = 3
ONLINE_TIMEOUT_SECONDS = 60
UPGRADE_TIMEOUT_SECONDS = 120
STOP_TIMEOUT_SECONDS = 45


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def run_quiet(command: list[str]) -> None:
    with contextlib.suppress(OSError):
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def daemon_command(release: Path, config_path: Path) -> list[str]:
    return [
        str(release / ".venv/bin/python"),
        "-m",
        "runtime_daemon.daemon",
        "--config",
        str(config_path),
    ]


def launchd_plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LAUNCHD_LABEL + ".plist")


def systemd_unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / (SERVICE_NAME + ".service")


def supervisor_script_path(config_path: Path) -> Path:
    return config_path.parent / "supervise.sh"


# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------


def detect_manager() -> str:
    if os.uname().sysname == "Darwin":
        return "launchd"
    probe = subprocess.run(
        [
            "sh",
            "-c",
            "command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1",
        ],
        check=False,
    )
    return "systemd" if probe.returncode == 0 else "supervised"


def manager_of(cfg: dict) -> str:
    status = str(cfg.get("service_status", ""))
    if status == "launchd":
        return "launchd"
    if status.startswith("systemd"):
        return "systemd"
    if status == "supervised":
        return "supervised"
    return detect_manager()


def launchd_definition(cfg: dict, config_path: Path) -> dict:
    release = Path(cfg["release"])
    service_log = str(config_path.parent / "service.log")
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": daemon_command(release, config_path),
        "WorkingDirectory": str(release),
        "RunAtLoad": True,
        # Restart unsuccessful exits only: on errors a restart cannot fix the daemon exits 0
        # under launchd, which has no per-status equivalent of RestartPreventExitStatus.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": cfg.get("path") or os.environ.get("PATH", ""),
        },
        # runtime.log is the main log. Only warnings and crash tracebacks reach this file,
        # and the daemon copy-truncates it while it runs.
        "StandardOutPath": service_log,
        "StandardErrorPath": service_log,
    }


def systemd_unit(cfg: dict, config_path: Path) -> str:
    release = Path(cfg["release"])
    return "\n".join(
        [
            "[Unit]",
            "Description=CoreMan Runtime",
            "After=network-online.target",
            "[Service]",
            "Type=simple",
            "WorkingDirectory=" + systemd_quote(str(release)),
            "Environment=" + systemd_quote("HOME=" + str(Path.home())),
            "Environment="
            + systemd_quote("PATH=" + (cfg.get("path") or os.environ.get("PATH", ""))),
            "ExecStart=" + " ".join(systemd_quote(x) for x in daemon_command(release, config_path)),
            "Restart=on-failure",
            "RestartSec=10",
            f"RestartPreventExitStatus={EXIT_CONFIG}",
            "TimeoutStopSec=30",
            "KillMode=control-group",
            "UMask=0077",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def supervisor_script(cfg: dict, config_path: Path) -> str:
    release = Path(cfg["release"])
    command = [
        str(release / ".venv/bin/python"),
        "-m",
        "runtime_daemon.supervisor",
        "--config",
        str(config_path),
    ]
    return (
        "#!/bin/sh\ncd "
        + shlex.quote(str(release))
        + "\nexec "
        + " ".join(shlex.quote(x) for x in command)
        + "\n"
    )


def systemd_status() -> str:
    try:
        linger = (
            subprocess.run(
                ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "yes"
        )
    except OSError:
        linger = False
    return "systemd-user" if linger else "systemd-user-session"


def launchd_domain() -> str:
    domain = f"gui/{os.getuid()}"
    probe = subprocess.run(
        ["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return domain if probe.returncode == 0 else f"user/{os.getuid()}"


def write_service(kind: str, cfg: dict, config_path: Path) -> None:
    if kind == "launchd":
        write_private(launchd_plist_path(), plistlib.dumps(launchd_definition(cfg, config_path)))
    elif kind == "systemd":
        write_private(systemd_unit_path(), systemd_unit(cfg, config_path))
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", SERVICE_NAME])
    else:
        script = supervisor_script_path(config_path)
        write_private(script, supervisor_script(cfg, config_path))
        script.chmod(0o700)


def start_service(kind: str, config_path: Path) -> None:
    if kind == "launchd":
        run(["launchctl", "bootstrap", launchd_domain(), str(launchd_plist_path())])
    elif kind == "systemd":
        # A unit that hit 78 stays "failed"; clear it so a fixed config can start again.
        run_quiet(["systemctl", "--user", "reset-failed", SERVICE_NAME])
        run(["systemctl", "--user", "restart", SERVICE_NAME])
    else:
        with (config_path.parent / "service.log").open("ab") as log:
            subprocess.Popen(
                [str(supervisor_script_path(config_path))],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )


def stop_supervisor(data_dir: Path, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
    lock = data_dir / "supervisor.lock"
    if not lock_held(lock):
        return
    try:
        pid = int((data_dir / "supervisor.pid").read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid <= 0:
        raise InstallError("监督进程正在运行但无法定位；请先停止宿主机托管的 supervise.sh 后重试")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    wait_for_unlock(lock, timeout)


def stop_service(kind: str, config_path: Path, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
    if kind == "launchd":
        for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            run_quiet(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
    elif kind == "systemd":
        run_quiet(["systemctl", "--user", "stop", SERVICE_NAME])
    else:
        stop_supervisor(config_path.parent, timeout)
    wait_for_unlock(config_path.parent / "daemon.lock", timeout)


def remove_service(kind: str, config_path: Path) -> None:
    """Best effort: undo ``write_service``/``start_service`` without raising."""
    if kind == "launchd":
        for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            run_quiet(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        launchd_plist_path().unlink(missing_ok=True)
    elif kind == "systemd":
        run_quiet(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        systemd_unit_path().unlink(missing_ok=True)
        run_quiet(["systemctl", "--user", "daemon-reload"])
        run_quiet(["systemctl", "--user", "reset-failed", SERVICE_NAME])
    else:
        with contextlib.suppress(InstallError, OSError):
            stop_supervisor(config_path.parent)
        supervisor_script_path(config_path).unlink(missing_ok=True)


def installed_kinds(config_path: Path, cfg: dict) -> list[str]:
    kinds = []
    if launchd_plist_path().exists():
        kinds.append("launchd")
    if systemd_unit_path().exists():
        kinds.append("systemd")
    if supervisor_script_path(config_path).exists() or lock_held(
        config_path.parent / "supervisor.lock"
    ):
        kinds.append("supervised")
    if not kinds and cfg.get("service_status") not in (None, "", "uninstalled"):
        kinds.append(manager_of(cfg))
    return kinds


def describe(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        command = exc.cmd if isinstance(exc.cmd, str) else " ".join(map(str, exc.cmd[:3]))
        return f"{command} 退出码 {exc.returncode}"
    return str(exc) or type(exc).__name__


def load_config(config_path: Path) -> dict:
    try:
        return json.loads(config_path.read_text())
    except FileNotFoundError:
        raise InstallError(f"未找到 Runtime 配置 {config_path}") from None
    except (OSError, ValueError) as exc:
        raise InstallError(f"无法读取 Runtime 配置 {config_path}：{describe(exc)}") from exc


def save_config(config_path: Path, cfg: dict) -> None:
    write_private(config_path, json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Install / upgrade / uninstall
# ---------------------------------------------------------------------------


def install(config_path: Path, staged_config: Path | None = None) -> int:
    """Register and start the service, then wait until the daemon reports online.

    With ``staged_config`` (install.sh) the identity file is created only after the service
    manager accepted the definition, and removed again if registration or enrollment fails,
    so the install command can simply be run again.
    """
    data_dir = config_path.parent
    if staged_config is not None:
        if config_path.exists():
            raise InstallError(
                "本机已安装 CoreMan Runtime，本次安装未做改动；重新运行安装命令查看处理方式。"
            )
        cfg = json.loads(staged_config.read_text())
    else:
        cfg = load_config(config_path)
    kind = detect_manager()
    cfg["service_status"] = {"launchd": "launchd", "supervised": "supervised"}.get(kind) or (
        systemd_status()
    )
    created = False
    try:
        write_service(kind, cfg, config_path)
        write_manage_command(config_path, Path(cfg["release"]))
        if staged_config is not None:
            create_private(config_path, json.dumps(cfg, indent=2))
            created = True
        else:
            save_config(config_path, cfg)
        started = time.time()
        start_service(kind, config_path)
    except (OSError, subprocess.SubprocessError, InstallError) as exc:
        remove_service(kind, config_path)
        if staged_config is not None:
            manage_command_path(config_path).unlink(missing_ok=True)
        if created:
            config_path.unlink(missing_ok=True)
        raise InstallError(f"服务注册失败（{describe(exc)}），已撤销本次注册。") from exc
    if kind == "systemd" and cfg["service_status"] == "systemd-user-session":
        print("用户服务已启动；无人登录时开机自启需由管理员启用该用户的 linger。")
    if kind == "supervised":
        print(
            "监督进程已启动；本环境无用户服务管理器。开机自启需由宿主机托管：",
            supervisor_script_path(config_path),
        )
    try:
        wait_online(data_dir, started, ONLINE_TIMEOUT_SECONDS)
    except FatalConfigError as exc:
        remove_service(kind, config_path)
        if staged_config is not None:
            manage_command_path(config_path).unlink(missing_ok=True)
        if created:
            config_path.unlink(missing_ok=True)
        raise InstallError(f"Runtime 无法完成注册，已撤销本次注册：{exc}") from exc
    except TimeoutError:
        print(
            "服务已安装，但尚未确认上线。请查看安装目录中的 runtime.log；"
            "修复后直接重启服务，无需重新兑换链接。",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIRMED
    print("Runtime 已注册并上线。请在运行时管理中查看 Claude/Codex 状态。")
    manage = shlex.quote(str(manage_command_path(config_path)))
    print(f"本机管理命令（升级、卸载等）：{manage} --help")
    return 0


def switch_release(config_path: Path, release: Path, kind: str) -> None:
    cfg = load_config(config_path)  # the daemon may have saved it since we last read it
    cfg["release"] = str(release)
    save_config(config_path, cfg)
    write_service(kind, cfg, config_path)
    write_manage_command(config_path, release)


def upgrade(config_path: Path, bundle: Path, sha256: str | None = None) -> None:
    data_dir = config_path.parent
    with install_lock(data_dir):
        cfg = load_config(config_path)
        if not cfg.get("release"):
            raise InstallError("Runtime 配置缺少 release，无法升级；请重新安装")
        old_release = Path(cfg["release"])
        kind = manager_of(cfg)
        expected = read_expected_sha256(bundle, sha256)
        data = load_bundle(bundle, expected)
        print("正在准备新版本（服务仍在运行）…", flush=True)
        try:
            new_release = build_release(data, data_dir, expected)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(f"新版本准备失败（{describe(exc)}），服务未受影响") from exc
        try:
            check_release(new_release)
        except InstallError:
            shutil.rmtree(new_release, ignore_errors=True)
            raise
        print("正在停止服务并切换版本…", flush=True)
        try:
            stop_service(kind, config_path)
        except InstallError:
            shutil.rmtree(new_release, ignore_errors=True)
            raise
        try:
            switch_release(config_path, new_release, kind)
            started = time.time()
            start_service(kind, config_path)
            wait_online(data_dir, started, UPGRADE_TIMEOUT_SECONDS)
        except (
            OSError,
            subprocess.SubprocessError,
            InstallError,
            FatalConfigError,
            TimeoutError,
        ) as exc:
            reason = describe(exc)
            print(f"新版本未能上线（{reason}），正在回滚…", file=sys.stderr, flush=True)
            restored = rollback(config_path, kind, old_release)
            shutil.rmtree(new_release, ignore_errors=True)
            if not restored:
                raise InstallError(
                    f"升级失败（{reason}），已切回 {old_release.name} 但尚未确认上线；"
                    "请查看 runtime.log"
                ) from exc
            raise InstallError(f"升级失败（{reason}），已回滚到 {old_release.name}") from exc
        removed = prune_releases(data_dir, keep=[new_release, old_release])
        print(
            f"升级完成：{new_release.name} 已上线；保留上一版本 {old_release.name} 以便手工回退，"
            f"清理 {len(removed)} 个更早的目录。"
        )


def rollback(config_path: Path, kind: str, release: Path) -> bool:
    try:
        stop_service(kind, config_path)
        switch_release(config_path, release, kind)
        started = time.time()
        start_service(kind, config_path)
        wait_online(config_path.parent, started, UPGRADE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, InstallError, FatalConfigError, TimeoutError):
        return False
    return True


def uninstall(config_path: Path, *, purge: bool = False) -> None:
    data_dir = config_path.parent
    if not data_dir.is_dir():
        print(f"未找到 Runtime 安装目录 {data_dir}，无需卸载。")
        return
    if purge and (
        data_dir.is_symlink()
        or data_dir in (Path("/"), Path.home(), Path(os.path.realpath(Path.home())))
        or not (config_path.exists() or data_dir == default_data_dir())
    ):
        raise InstallError(f"{data_dir} 不像 Runtime 安装目录，拒绝删除")
    with install_lock(data_dir):
        cfg = read_config_leniently(config_path)
        stop_runtime(config_path, cfg)
        shutil.rmtree(data_dir / "cli-bin", ignore_errors=True)
        for name in ("state.json", "trust.pem", "supervisor.pid", "supervisor.lock"):
            (data_dir / name).unlink(missing_ok=True)
        if purge:
            shutil.rmtree(data_dir)
            print(f"已删除 {data_dir}（含节点身份、会话与日志）。请在「运行时管理」中撤销该节点。")
            return
        if not cfg:
            print(f"已停止并移除 Runtime 服务；{data_dir} 中没有可用的节点配置。")
            return
        current = [Path(cfg["release"])] if cfg.get("release") else []
        prune_releases(data_dir, keep=current)
        cfg["service_status"] = "uninstalled"
        save_config(config_path, cfg)
        if current:
            write_manage_command(config_path, current[0])
        manage = shlex.quote(str(manage_command_path(config_path)))
        print(
            f"已停止并移除 Runtime 服务；节点身份、会话与日志保留在 {data_dir}。\n"
            f"  恢复服务：{manage} --register\n"
            f"  彻底删除：{manage} --uninstall --purge"
        )


def read_config_leniently(config_path: Path) -> dict:
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def stop_runtime(config_path: Path, cfg: dict) -> None:
    """Stop and unregister the service; the caller holds the install lock."""
    data_dir = config_path.parent
    for kind in installed_kinds(config_path, cfg):
        with contextlib.suppress(InstallError):
            stop_service(kind, config_path)
        remove_service(kind, config_path)
    if lock_held(data_dir / "daemon.lock"):
        raise InstallError("Runtime 进程仍在运行（可能由宿主机托管）；请先停止后重试")
    if cfg.get("node_id"):
        remove_socket_dirs(data_dir, str(cfg["node_id"]))


def retire(config_path: Path, backup: Path) -> None:
    """``install.sh --replace``: stop the installed runtime and move its files into ``backup``.

    install.sh holds the install lock and runs this from the new bundle, so an old or broken
    release does not matter. The backup is created before the service is touched: renames
    only work within one filesystem, and a data directory bind-mounted under a read-only
    ``~/.local/share`` has no writable sibling. The lock and install.sh's stage directories
    stay; config.json moves last, so an interrupted move still reads as an installed runtime.
    """
    data_dir = config_path.parent
    unchanged = (
        "未改动现有 Runtime；请先用 coreman-runtime --uninstall --purge 卸载，再不带 --replace 安装"
    )
    try:
        backup.mkdir(mode=0o700)
    except OSError as exc:
        raise InstallError(f"无法创建备份目录 {backup}（{describe(exc)}），{unchanged}") from exc
    try:
        if backup.stat().st_dev != data_dir.stat().st_dev:
            raise InstallError(f"备份目录 {backup} 与安装目录不在同一文件系统，{unchanged}")
        stop_runtime(config_path, read_config_leniently(config_path))
    except BaseException:
        backup.rmdir()
        raise
    try:
        for child in sorted(data_dir.iterdir(), key=lambda path: path == config_path):
            if child.name != "install.lock" and not child.name.startswith("stage-"):
                child.rename(backup / child.name)
    except OSError as exc:
        raise InstallError(
            f"备份现有 Runtime 失败（{describe(exc)}）；服务已停止，已移动的文件在 {backup}"
        ) from exc


MAIN_EPILOG = """示例：
  coreman-runtime --upgrade ./coreman-runtime-darwin-arm64.tar.gz   升级并保留节点身份
  coreman-runtime --uninstall            停止并移除服务，保留节点身份、会话与日志
  coreman-runtime --uninstall --purge    同时删除节点身份、会话与日志
  coreman-runtime --register             卸载服务后，按保留的身份重新注册
改连其他平台或换新身份：把安装命令末尾的 | sh 换成 | sh -s -- --replace"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CoreMan Runtime 管理命令：升级、卸载与重新注册服务",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, help="config.json 路径，默认为用户安装目录")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--upgrade", type=Path, metavar="BUNDLE", help="发布包 .tar.gz")
    action.add_argument("--uninstall", action="store_true", help="停止并移除服务")
    action.add_argument(
        "--register", action="store_true", help="按现有配置注册并启动服务（--uninstall 之后恢复）"
    )
    action.add_argument("--retire", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sha256", help="发布包校验值；缺省读取同目录 <BUNDLE>.sha256")
    parser.add_argument("--purge", action="store_true", help="卸载时同时删除身份与数据")
    parser.add_argument("--staged-config", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.purge and not args.uninstall:
        parser.error("--purge 只能与 --uninstall 一起使用")
    if args.sha256 and not args.upgrade:
        parser.error("--sha256 只能与 --upgrade 一起使用")
    config_path = (args.config or default_data_dir() / "config.json").absolute()
    try:
        if args.upgrade:
            upgrade(config_path, args.upgrade.absolute(), args.sha256)
        elif args.uninstall:
            uninstall(config_path, purge=args.purge)
        elif args.retire:
            retire(config_path, args.retire.absolute())
        else:
            return install(config_path, args.staged_config)
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
