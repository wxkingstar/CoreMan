"""Foreground process supervisor for chroot environments without an init system."""

import argparse
import fcntl
import json
import signal
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    path = parser.parse_args().config
    cfg = json.loads(path.read_text())
    release = Path(cfg["release"])
    with (path.parent / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopped = False
        process = None

        def stop(*_):
            nonlocal stopped
            stopped = True
            if process and process.poll() is None:
                process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopped:
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
            process.wait()
            if not stopped:
                time.sleep(10)


if __name__ == "__main__":
    main()
