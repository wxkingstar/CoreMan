"""Install the current user's service without altering an existing CLI login."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import time
from pathlib import Path

from relay_agent.agent import atomic_write


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install(config_path: Path) -> None:
    cfg = json.loads(config_path.read_text())
    release = Path(cfg["release"])
    command = [
        str(release / ".venv/bin/python"),
        "-m",
        "runtime_daemon.daemon",
        "--config",
        str(config_path),
    ]
    name = "coreman-runtime"
    if os.uname().sysname == "Darwin":
        path = Path.home() / "Library/LaunchAgents/org.coreman.runtime.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        if subprocess.run(
            ["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode:
            domain = f"user/{os.getuid()}"
        data = {
            "Label": "org.coreman.runtime",
            "ProgramArguments": command,
            "WorkingDirectory": str(release),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": cfg["path"]},
            "StandardOutPath": str(config_path.parent / "service.log"),
            "StandardErrorPath": str(config_path.parent / "service.log"),
        }
        atomic_write(path, plistlib.dumps(data).decode())
        cfg["service_status"] = "launchd"
        atomic_write(config_path, json.dumps(cfg))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    elif (
        subprocess.run(
            [
                "sh",
                "-c",
                "command -v systemctl >/dev/null && "
                "systemctl --user show-environment >/dev/null 2>&1",
            ],
            check=False,
        ).returncode
        == 0
    ):
        path = Path.home() / ".config/systemd/user/coreman-runtime.service"
        unit = "\n".join(
            [
                "[Unit]",
                "Description=CoreMan Runtime",
                "After=network-online.target",
                "[Service]",
                "Type=simple",
                "WorkingDirectory=" + systemd_quote(str(release)),
                "Environment=" + systemd_quote("HOME=" + str(Path.home())),
                "Environment=" + systemd_quote("PATH=" + cfg["path"]),
                "ExecStart=" + " ".join(systemd_quote(x) for x in command),
                "Restart=on-failure",
                "RestartSec=10",
                "TimeoutStopSec=30",
                "KillMode=control-group",
                "UMask=0077",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        atomic_write(path, unit)
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
        cfg["service_status"] = "systemd-user" if linger else "systemd-user-session"
        atomic_write(config_path, json.dumps(cfg))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True)
        if not linger:
            print("用户服务已启动；无人登录时开机自启需由管理员启用该用户的 linger。")
    else:
        # A chroot with no init cannot register a boot service inside itself.
        # Ship an executable foreground supervisor for the host's existing service manager.
        cfg["service_status"] = "supervised"
        atomic_write(config_path, json.dumps(cfg))
        script = config_path.parent / "supervise.sh"
        atomic_write(
            script,
            "#!/bin/sh\ncd "
            + shlex.quote(str(release))
            + "\nexec "
            + " ".join(
                shlex.quote(x)
                for x in [
                    command[0],
                    "-m",
                    "runtime_daemon.supervisor",
                    "--config",
                    str(config_path),
                ]
            )
            + "\n",
        )
        script.chmod(0o700)
        with (config_path.parent / "service.log").open("ab") as log:
            subprocess.Popen(
                [str(script)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print("监督进程已启动；本环境无用户服务管理器。开机自启需由宿主机托管：", script)
    state_path = config_path.parent / "state.json"
    for _ in range(60):
        try:
            state = json.loads(state_path.read_text())
            if state.get("online") and time.time() - state.get("updated_at", 0) < 20:
                print("Runtime 已注册并上线。请在运行时管理中查看 Claude/Codex 状态。")
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise SystemExit(
        "服务已安装，但尚未确认上线。请查看安装目录中的 service.log/runtime.log；"
        "修复后直接重启服务，无需重新兑换链接。"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    install(parser.parse_args().config)
