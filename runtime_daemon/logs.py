"""Bounded log files for months of unattended operation.

The daemon owns ``runtime.log`` and rotates it by renaming. Driver logs and the service
manager's ``service.log`` are held open by another process with ``O_APPEND``; those are
copy-truncated in place, so the writer's next line lands at the new end of the file.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import logging.handlers
import os
import shutil
import stat
import sys
import threading
from pathlib import Path

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
ROTATE_INTERVAL_SECONDS = 60.0
# httpx logs one INFO line per request; at two requests per second that dominated the log.
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3")
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

LOG = logging.getLogger("coreman-runtime.logs")


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


def configure_logging(data_dir: Path, *, console_level: int | None = None) -> list[logging.Handler]:
    """Main log to ``runtime.log``; only warnings reach stderr unless it is a terminal."""
    formatter = logging.Formatter(FORMAT)
    main = PrivateRotatingFileHandler(
        data_dir / "runtime.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    main.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    if console_level is None:
        console_level = logging.INFO if sys.stderr.isatty() else logging.WARNING
    console.setLevel(console_level)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(main)
    root.addHandler(console)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return [main, console]


def rotate_copytruncate(
    path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS
) -> bool:
    """Rotate a file another process appends to without asking it to reopen.

    Lines written between the copy and the truncate are lost; that window is one copy of
    at most ``max_bytes`` every rotation.
    """
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except FileNotFoundError:
        return False
    if backups > 0:
        for index in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
        backup = path.with_name(path.name + ".1")
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    os.truncate(path, 0)
    return True


def appended_stderr_file(expected: Path) -> Path | None:
    """Return ``expected`` when this process's stderr is that file opened for append.

    launchd and the chroot supervisor point stderr at ``service.log``; truncating is only
    safe when the descriptor appends, otherwise the next write would leave a sparse hole.
    """
    try:
        descriptor = os.fstat(2)
        if not stat.S_ISREG(descriptor.st_mode):
            return None
        if not fcntl.fcntl(2, fcntl.F_GETFL) & os.O_APPEND:
            return None
        target = expected.stat()
    except OSError:
        return None
    if (descriptor.st_dev, descriptor.st_ino) != (target.st_dev, target.st_ino):
        return None
    return expected


class LogRotator:
    """Periodically copy-truncates files whose writers never restart on their own."""

    def __init__(self, interval: float = ROTATE_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self.targets: dict[Path, tuple[int, int]] = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def watch(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        with self.lock:
            self.targets[path] = (max_bytes, backups)

    def rotate(self) -> None:
        with self.lock:
            targets = list(self.targets.items())
        for path, (max_bytes, backups) in targets:
            try:
                rotate_copytruncate(path, max_bytes, backups)
            except OSError as exc:
                LOG.warning("Log rotation failed for %s: %s", path.name, exc)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="log-rotator", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.stopped.wait(self.interval):
            self.rotate()

    def stop(self) -> None:
        self.stopped.set()
