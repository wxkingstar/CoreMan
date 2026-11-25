"""Remote command line: commands running at the same time on one computer share the login.

The refresh token rotates and the server revokes the whole grant when an old one shows up again,
so only one of them may refresh; the others must pick up the tokens it saved.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from coreman.admin_cli import remote

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ID = "cli-1"

# One `coreman` command: loads the login, waits until every command has it in memory, then calls
# the API through the normal session (which refreshes the expired token first).
COMMAND = """
import asyncio, sys
from coreman.admin_cli import remote

async def main():
    creds = remote.load()
    print("ready", flush=True)
    sys.stdin.readline()
    async with remote.session(creds) as http:
        r = await http.get("/api/admin/auth/me")
    print(r.status_code, r.json()["data"]["token"], flush=True)

asyncio.run(main())
"""


class FakeOAuth:
    """Token endpoint with the same rotation and reuse detection as the real server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0
        self.previous: str | None = None
        self.revoked = False
        self.refreshes = 0
        # Hold every refresh a moment so concurrent commands really overlap.
        self.delay = 0.3

    @property
    def access(self) -> str:
        return f"at-{self.generation}"

    @property
    def refresh(self) -> str:
        return f"rt-{self.generation}"

    def rotate(self, token: str) -> tuple[int, dict[str, object]]:
        time.sleep(self.delay)
        with self.lock:
            self.refreshes += 1
            if token == self.previous:
                self.revoked = True
            if self.revoked or token != self.refresh:
                return 400, {"error": "invalid_grant", "error_description": "续期令牌无效"}
            self.previous = token
            self.generation += 1
            return 200, {
                "access_token": self.access,
                "refresh_token": self.refresh,
                "token_type": "Bearer",
                "expires_in": 3600,
            }

    def me(self, authorization: str) -> tuple[int, dict[str, object]]:
        with self.lock:
            if self.revoked or authorization != f"Bearer {self.access}":
                return 401, {"data": None}
            return 200, {"data": {"token": self.access}}


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[FakeOAuth, str]]:
    fake = FakeOAuth()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            assert self.path == "/api/oauth/token"
            assert form["grant_type"] == "refresh_token" and form["client_id"] == CLIENT_ID
            self._send(*fake.rotate(form["refresh_token"]))

        def do_GET(self) -> None:
            assert self.path == "/api/admin/auth/me"
            self._send(*fake.me(self.headers.get("Authorization", "")))

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(remote.CONFIG_ENV, str(tmp_path / "cfg"))
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _saved(server: str, access: str, refresh: str, expires_at: float) -> remote.Credentials:
    creds = remote.Credentials(server, CLIENT_ID, access, refresh, expires_at)
    remote.save(creds)
    return remote.Credentials(server, CLIENT_ID, access, refresh, expires_at)


def test_parallel_commands_refresh_once(oauth: tuple[FakeOAuth, str]) -> None:
    fake, server = oauth
    _saved(server, "at-0", "rt-0", expires_at=0)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", COMMAND],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=ROOT,
        )
        for _ in range(4)
    ]
    try:
        # Every command has the expired login in memory before any of them refreshes.
        for proc in procs:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready", proc.stderr and proc.stderr.read()
        for proc in procs:
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [proc.communicate(timeout=60) for proc in procs]
    finally:
        for proc in procs:
            proc.kill()
    for proc, (out, err) in zip(procs, results, strict=True):
        assert proc.returncode == 0, err
        assert out.split() == ["200", "at-1"], err
    assert fake.refreshes == 1
    assert not fake.revoked
    saved = remote.load()
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == ("at-1", "rt-1")
    assert stat.S_IMODE(remote.config_path().stat().st_mode) == 0o600


async def test_concurrent_requests_in_one_process_refresh_once(
    oauth: tuple[FakeOAuth, str],
) -> None:
    fake, server = oauth
    first = _saved(server, "at-0", "rt-0", expires_at=0)
    second = remote.Credentials(**vars(first))

    async def call(creds: remote.Credentials) -> str:
        async with remote.session(creds) as http:
            r = await http.get("/api/admin/auth/me")
        assert r.status_code == 200
        token: str = r.json()["data"]["token"]
        return token

    assert list(await asyncio.gather(call(first), call(second))) == ["at-1", "at-1"]
    assert fake.refreshes == 1


async def test_rejected_token_picks_up_login_refreshed_elsewhere(
    oauth: tuple[FakeOAuth, str],
) -> None:
    """Another command already refreshed: a 401 on the old access token reuses its new tokens."""
    fake, server = oauth
    fake.generation, fake.previous = 1, "rt-0"
    _saved(server, "at-1", "rt-1", expires_at=time.time() + 3600)
    stale = remote.Credentials(server, CLIENT_ID, "at-0", "rt-0", time.time() + 1800)
    async with remote.session(stale) as http:
        r = await http.get("/api/admin/auth/me")
    assert r.status_code == 200 and r.json()["data"]["token"] == "at-1"
    assert fake.refreshes == 0
    assert not fake.revoked
