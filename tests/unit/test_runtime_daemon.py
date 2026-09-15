import asyncio
import base64
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_daemon.agent import COMMAND_CANCEL, Agent, OperationError, run_command
from runtime_daemon.daemon import Daemon
from runtime_daemon.install_service import systemd_quote


@pytest.mark.parametrize("silence,cancelled", [(5, False), (12, False), (25, False), (45, True)])
async def test_control_lease_does_not_cancel_short_outages(daemon, monkeypatch, silence, cancelled):
    import runtime_daemon.daemon as module

    daemon.control_deadline = 40
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: silence))
    active = asyncio.create_task(asyncio.sleep(60))
    daemon.tasks["one"] = active
    watcher = asyncio.create_task(daemon.watch_control_lease())
    try:
        await asyncio.sleep(0.03)
        assert active.cancelled() == cancelled
        assert ("one" in daemon.abandoned) == cancelled
    finally:
        daemon.stopping.set()
        watcher.cancel()
        active.cancel()
        await asyncio.gather(watcher, active, return_exceptions=True)


async def test_frame_retry_deadline_includes_inflight_request(daemon, monkeypatch):
    import httpx

    import runtime_daemon.daemon as module

    async def stalled(*args, **kwargs):
        await asyncio.sleep(60)

    daemon.http = SimpleNamespace(post=stalled)
    monkeypatch.setattr(module, "FRAME_RETRY_SECONDS", 0.03)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        await daemon.api("/frames", {"seq": 1}, retry=True)
    assert time.monotonic() - started < 0.5


@pytest.fixture
def daemon(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspace_root": str(root), "api_url": "http://localhost"}))
    node = Daemon(path)
    node.agents["claude"] = Agent(
        root=root, api_url="http://localhost", token="x" * 32, relay_id="test", home=tmp_path
    )
    return node


def command(data, path="/v1/chat/completions"):
    return {
        "id": "test",
        "provider": "claude",
        "method": "POST",
        "path": path,
        "body": base64.b64encode(json.dumps(data).encode()).decode(),
    }


def test_workspace_and_identity_protection(daemon, tmp_path):
    root = Path(daemon.config["workspace_root"])
    result = daemon.validate_command(
        command({"working_dir": str(root / "hello world"), "session_id": "test-1"})
    )
    assert result["working_dir"] == str(root / "hello world")
    (root / "escape").symlink_to(tmp_path)
    for payload in [
        {"working_dir": str(root / "escape/x")},
        {"working_dir": str(root / "hello"), "env_vars": {"HOME": "/root"}},
        {"working_dir": str(root / "hello"), "session_id": "../../secret"},
        {"working_dir": str(root / "hello"), "add_dirs": [str(tmp_path)]},
    ]:
        with pytest.raises((ValueError, OperationError)):
            daemon.validate_command(command(payload))
    with pytest.raises(ValueError):
        daemon.validate_command(command({}, path="http://example.test/"))


async def test_management_cancel_kills_child_process_group(tmp_path):
    cancelled = threading.Event()
    token = COMMAND_CANCEL.set(cancelled)
    try:
        task = asyncio.create_task(
            asyncio.to_thread(
                run_command, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=60
            )
        )
        await asyncio.sleep(0.2)
        started = time.monotonic()
        cancelled.set()
        with pytest.raises(OperationError):
            await task
        assert time.monotonic() - started < 5
    finally:
        COMMAND_CANCEL.reset(token)


def test_status_command_does_not_override_global_claude_config(daemon, tmp_path):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"statusLine":{"command":"custom"},"theme":"dark"}')
    daemon.agents["claude"].install_claude_probe()
    assert (
        json.loads(settings.with_name("settings.before-coreman-probe.json").read_text())[
            "statusLine"
        ]["command"]
        == "custom"
    )
    assert json.loads(settings.read_text())["theme"] == "dark"
    assert systemd_quote('a b%"') == '"a b%%\\""'


def test_cli_detection_keeps_unknown_distinct(monkeypatch):
    from runtime_daemon.daemon import cli_status

    monkeypatch.setattr("runtime_daemon.daemon.shutil.which", lambda _: "/fake/codex")
    responses = iter(
        [
            SimpleNamespace(stdout="codex test", stderr="", returncode=0),
            SimpleNamespace(stdout="", stderr="not logged in", returncode=1),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(responses))
    assert cli_status("codex")["login"] == "required"


def test_no_persistent_claude_modes_in_release_source():
    root = Path(__file__).parents[2] / "runtime_daemon/drivers/cmd/relay-claude"
    assert not list(root.glob("channel*"))
    assert not list(root.glob("v3*"))
    source = (root / "main.go").read_text()
    assert "channelv3" not in source
    assert "newChanManager" not in source
    assert '"socket"' in source


@pytest.mark.parametrize("provider,mode", [("claude", "plan"), ("codex", "read-only")])
def test_health_probe_uses_provider_permission_mode(daemon, monkeypatch, provider, mode):
    from runtime_daemon.daemon import RuntimeAgent

    daemon.socket_dir = daemon.data_dir
    daemon.config["node_token"] = "test-token"
    daemon.config["home"] = str(daemon.data_dir)
    captured = []

    class LocalClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path):
            return SimpleNamespace(status_code=200)

        def post(self, path, json):
            captured.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {"choices": []})

    monkeypatch.setattr("runtime_daemon.daemon.httpx.Client", LocalClient)
    agent = RuntimeAgent(daemon, provider, "test-relay")
    agent.model = "test-model"
    reports = []
    monkeypatch.setattr(agent, "report", lambda endpoint, body: reports.append(body))
    agent.health_check()
    assert captured[0]["permission_mode"] == mode
    assert reports[0]["status"] == "healthy"
