"""Foreground process supervisor for chroot environments without an init system."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from runtime_daemon.lifecycle import EXIT_CONFIG

RESTART_DELAY_SECONDS = 10.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    path = parser.parse_args(argv).config
    with (path.parent / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("监督进程已在运行", file=sys.stderr)
            return 0
        # install_service --upgrade/--uninstall stops this process through the pid file.
        pid_file = path.parent / "supervisor.pid"
        pid_file.write_text(str(os.getpid()))
        stopped = False
        process = None

        def stop(*_):
            nonlocal stopped
            stopped = True
            if process and process.poll() is None:
                process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopped:
                # Re-read every run: an upgrade may have switched the release.
                release = Path(json.loads(path.read_text())["release"])
                process = subprocess.Popen(
                    [
                        str(release / ".venv/bin/python"),
                        "-m",
                        "runtime_daemon.daemon",
                        "--config",
                        str(path),
                    ],
                    cwd=release,
                )
                if stopped:
                    process.terminate()
                code = process.wait()
                if stopped:
                    break
                if code == EXIT_CONFIG:
                    print(
                        "Runtime 因重启无法解决的错误停止，监督进程不再重启；详情见 runtime.log",
                        file=sys.stderr,
                    )
                    return EXIT_CONFIG
                deadline = time.monotonic() + RESTART_DELAY_SECONDS
                while not stopped and time.monotonic() < deadline:
                    time.sleep(0.2)
        finally:
            pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
