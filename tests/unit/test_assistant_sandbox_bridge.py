"""沙箱里的转发口：127.0.0.1 上的连接原样转到 Unix socket，退出码随看护的子进程。"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "runtime_daemon" / "sandbox_bridge.py"
CLIENT = """
import socket, sys, time
for _ in range(50):
    try:
        s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
        break
    except OSError:
        time.sleep(0.1)
s.sendall(b"ping")
print(s.recv(64).decode())
import os
mask = os.umask(0)
print(oct(mask))
sys.exit(7)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def upstream(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"pong:" + conn.recv(64))

    threading.Thread(target=serve, daemon=True).start()
    return server


def test_bridge_forwards_to_the_upstream_socket_and_returns_the_child_code():
    path = Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"
    server = upstream(path)
    port = free_port()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(BRIDGE),
                "--port",
                str(port),
                "--upstream",
                str(path),
                "--",
                sys.executable,
                "-c",
                CLIENT,
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.close()
    assert result.returncode == 7
    # 被看护的进程继承 077 的 umask：这一轮写下的文件只有本用户读得到。
    assert result.stdout.split() == ["pong:ping", "0o77"]


def test_bridge_needs_a_command():
    result = subprocess.run(
        [sys.executable, "-I", str(BRIDGE), "--port", "1", "--upstream", "/nonexistent"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0


def test_bridge_only_uses_the_standard_library():
    source = BRIDGE.read_text()
    assert "runtime_daemon" not in source.split('"""', 2)[2]
    assert "import httpx" not in source
