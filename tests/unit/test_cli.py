import base64
import os
import subprocess
import sys


def test_cli_help_lists_contact_sync() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "coreman.cli", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0 and "contact-sync" in r.stdout


def test_cli_logs_go_to_stderr_not_stdout() -> None:
    """CLI 的结果 JSON 走 stdout，structlog 日志必须走 stderr，两者不能混在一起。"""
    r = subprocess.run(
        [sys.executable, "-m", "coreman.cli", "contact-sync", "--app", "nope"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/nope",
            "PUBLIC_BASE_URL": "http://x",
            "MASTER_KEY": base64.b64encode(b"\x01" * 32).decode(),
            "SESSION_SECRET": "s" * 32,
        },
    )
    assert r.returncode == 1
    assert '"event"' not in r.stdout  # structlog JSON 行不再进 stdout
