"""Real Daemon + native Go drivers against an isolated API/Postgres instance.

AI CLI executables are deterministic fakes; no real account or inference is used.
Build native bundles first (runtime_daemon/build.py). CI Docker builds all targets.
"""

import asyncio
import json
import os
import platform
import socket
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
import uvicorn

from coreman.core.db.models import RuntimeNode
from coreman.core.db.session import make_session_factory
from coreman.core.runtime_nodes.transport import ReverseTransport
from runtime_daemon.daemon import Daemon
from tests.api.conftest import login_as

FAKE_CLI = """
import json, sys
if '--version' in sys.argv:
 print('test-cli 1.0'); sys.exit()
if 'auth' in sys.argv:
 print(json.dumps({'loggedIn': True})); sys.exit()
if 'login' in sys.argv:
 print('Logged in using ChatGPT'); sys.exit()
content = sys.stdin.read()
provider = sys.argv[0].split('/')[-1]
if provider == 'claude':
 events = [
  {'type':'system','subtype':'init','session_id':'test'},
  {'type':'assistant','message':{'role':'assistant',
   'content':[{'type':'text','text':'Claude runtime OK'}]}},
  {'type':'result','subtype':'success','result':'Claude runtime OK',
   'usage':{'input_tokens':10,'output_tokens':3}}
 ]
else:
 events = [
  {'type':'thread.started','thread_id':'fake-thread'},
  {'type':'item.completed','item':{'id':'msg','type':'agent_message','text':'Codex runtime OK'}},
  {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3,'cached_input_tokens':0}}
 ]
for event in events:
 print(json.dumps(event), flush=True)
"""


async def test_real_daemon_dual_provider_roundtrip(
    app, client, db_session, db_engine, tmp_path, monkeypatch
):
    system = platform.system().lower()
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"
    bundle = (
        Path(__file__).parents[2] / f"runtime_daemon/dist/coreman-runtime-{system}-{arch}.tar.gz"
    )
    if not bundle.exists():
        pytest.skip("build the native Runtime bundle to run the executable smoke test")
    release = tmp_path / "release"
    release.mkdir()
    with tarfile.open(bundle) as archive:
        archive.extractall(release, filter="data")
    root = tmp_path / "projects"
    root.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for provider in ("claude", "codex"):
        path = fake_bin / provider
        path.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        path.chmod(0o700)
    await login_as(client, db_session, role="platform_admin")
    link = (
        await client.post(
            "/api/admin/runtime-nodes/install-links", json={"workspace_root": str(root)}
        )
    ).json()["data"]
    token = link["url"].split("/install/")[1].split("/")[0]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110 uvicorn exposes a boolean readiness flag
        await asyncio.sleep(0.02)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "api_url": f"http://127.0.0.1:{sock.getsockname()[1]}",
                "install_token": token,
                "workspace_root": str(root),
                "release": str(release),
                "schedules": False,
                "home": str(tmp_path),
                "claude_path": str(fake_bin / "claude"),
                "codex_path": str(fake_bin / "codex"),
            }
        )
    )
    monkeypatch.setenv("PATH", os.environ["PATH"])  # restore after Daemon selects its CLI paths
    daemon = Daemon(config)
    running = asyncio.create_task(daemon.run())
    try:
        for _ in range(300):
            if running.done():
                await running
            if daemon.config.get("backends") and all(
                daemon.capabilities[p].get("models") for p in ("claude", "codex")
            ):
                break
            await asyncio.sleep(0.05)
        assert daemon.config.get("backends")
        assert "install_token" not in json.loads(config.read_text())
        import uuid

        identity = uuid.UUID(daemon.config["node_id"])
        for provider in ("claude", "codex"):
            transport = ReverseTransport(
                identity, provider, factory=make_session_factory(db_engine), cipher=app.state.cipher
            )
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://runtime/{provider}", timeout=10
            ) as caller:
                response = await caller.post(
                    "/v1/chat/completions",
                    json={
                        "model": daemon.capabilities[provider]["models"][0],
                        "stream": False,
                        "working_dir": str(root / provider),
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
                assert response.status_code == 200, response.text
                assert f"{provider.title()} runtime OK" in response.text
                response = await caller.post("/", json={"type": "ping"})
                assert response.json()["memory_protocol"] == 2
        async with make_session_factory(db_engine)() as session:
            node = await session.get(RuntimeNode, identity)
            assert node and node.platform == system
        assert len(daemon.drivers) == 2
    finally:
        daemon.stopping.set()
        await asyncio.wait_for(running, 20)
        assert all(process.poll() is not None for process in daemon.drivers.values())
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()
