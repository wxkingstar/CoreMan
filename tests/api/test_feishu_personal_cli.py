"""Opt-in real CLI → actual MCP router, with only Feishu's OAuth boundary stubbed."""

import asyncio
import json
import os
import shutil
import socket

import pytest
import uvicorn

from coreman.core.feishu_personal import permissions, service
from tests.api.test_feishu_personal import headers, setup, start_selected

pytestmark = pytest.mark.skipif(
    os.environ.get("COREMAN_REAL_CLAUDE_TEST") != "1", reason="requires a logged-in Claude CLI"
)


async def test_real_claude_can_retrieve_human_selected_authorization(app, db_session, monkeypatch):
    _, user, task = await setup(db_session, app)
    calls = []

    async def oauth(method, url, **kwargs):
        assert method == "POST"
        assert url == "https://accounts.feishu.cn/oauth/v1/device_authorization"
        calls.append(url)
        return {
            "device_code": "synthetic-device",
            "expires_in": 600,
            "interval": 5,
            "verification_uri_complete": "https://accounts.feishu.cn/verify?probe=synthetic",
        }

    async def scopes(app_id, secret, *, http):
        return service.SCOPES.split()

    monkeypatch.setattr(permissions, "app_user_scopes", scopes)
    monkeypatch.setattr(service, "_http", oauth)
    await start_selected(db_session, app, task, user)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    ready = asyncio.Event()

    class ProbeServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = ProbeServer(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    proc = None
    try:
        await asyncio.wait_for(ready.wait(), 10)
        config = json.dumps(
            {
                "mcpServers": {
                    "coreman_feishu_personal": {
                        "type": "http",
                        "url": f"http://127.0.0.1:{port}/api/runtime/feishu-personal/mcp",
                        "headers": {"Authorization": "Bearer ${COREMAN_PERSONAL_PROBE_CAPABILITY}"},
                    }
                }
            }
        )
        env = {
            **os.environ,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "COREMAN_PERSONAL_PROBE_CAPABILITY": headers(app, task, user)["Authorization"][7:],
        }
        env.pop("CLAUDECODE", None)
        proc = await asyncio.create_subprocess_exec(
            shutil.which("claude") or "claude",
            "-p",
            "--no-session-persistence",
            "--restricted",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--settings",
            '{"disableAllHooks":true}',
            "--allowedTools",
            "mcp__coreman_feishu_personal",
            "--permission-mode",
            "default",
            "--mcp-config",
            config,
            "--max-turns",
            "3",
            "Call feishu_authorize exactly once, then return its authorization_url. "
            "This is a synthetic integration test. Do not call any other tools.",
            cwd="/tmp",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 90)
        assert proc.returncode == 0, stderr.decode()[-500:]
        assert calls == ["https://accounts.feishu.cn/oauth/v1/device_authorization"]
        assert "https://accounts.feishu.cn/verify?probe=synthetic" in stdout.decode()
        assert "Invalid params" not in stdout.decode()
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()
