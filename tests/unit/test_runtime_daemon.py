import asyncio
import base64
import json
import os
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


def test_root_change_is_durable_and_updates_agents(daemon, tmp_path):
    target = str(tmp_path / "new-projects")
    daemon.apply_root_change({"id": "change-1", "path": target, "status": "pending"})
    assert daemon.agents["claude"].root == Path(target)
    saved = json.loads(daemon.config_path.read_text())
    assert saved["workspace_root"] == target
    assert saved["root_change_result"] == {"id": "change-1", "path": target, "status": "applied"}
    assert (
        Daemon(daemon.config_path).heartbeat_body()["root_change_result"]
        == saved["root_change_result"]
    )


def test_root_change_rejects_symlink_and_keeps_old_root(daemon, tmp_path):
    old = daemon.config["workspace_root"]
    (tmp_path / "link").symlink_to(tmp_path / "projects")
    daemon.apply_root_change({"id": "bad", "path": str(tmp_path / "link"), "status": "pending"})
    assert daemon.config["workspace_root"] == old
    assert daemon.agents["claude"].root == Path(old)
    assert daemon.config["root_change_result"]["status"] == "failed"


def test_root_change_symlink_loop_does_not_kill_heartbeats(daemon, tmp_path):
    old = daemon.config["workspace_root"]
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    daemon.apply_root_change({"id": "loop", "path": str(loop), "status": "pending"})
    assert daemon.config["workspace_root"] == old
    assert daemon.heartbeat_body()["root_change_result"]["status"] == "failed"


def test_root_change_write_failure_does_not_ack_or_change_agents(daemon, tmp_path, monkeypatch):
    old = daemon.config["workspace_root"]

    def unwritable():
        raise OSError("read-only disk")

    monkeypatch.setattr(daemon, "save", unwritable)
    daemon.apply_root_change(
        {"id": "write-failure", "path": str(tmp_path / "new"), "status": "pending"}
    )
    assert daemon.config["workspace_root"] == old
    assert daemon.agents["claude"].root == Path(old)
    assert "root_change_result" not in daemon.config


def test_root_change_flushes_config_and_parent_before_ack(daemon, tmp_path, monkeypatch):
    import os
    import stat

    synced = []
    original = os.fsync

    def sync(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    daemon.apply_root_change(
        {"id": "durable", "path": str(tmp_path / "durable"), "status": "pending"}
    )
    assert synced == ["file", "directory"]
    assert daemon.heartbeat_body()["root_change_result"]["status"] == "applied"


def test_root_change_replayed_after_restart_is_not_applied_again(daemon, tmp_path):
    target = str(tmp_path / "new")
    change = {"id": "once", "path": target, "status": "pending"}
    daemon.apply_root_change(change)
    restarted = Daemon(daemon.config_path)
    # The root is now temporarily unwritable/non-directory, but an old request
    # is only acknowledged again, never re-executed or changed into a failure.
    Path(target).rmdir()
    Path(target).write_text("blocked")
    restarted.apply_root_change(change)
    assert restarted.config["root_change_result"]["status"] == "applied"


async def test_root_change_waits_for_active_work(daemon, tmp_path):
    old = daemon.config["workspace_root"]
    active = asyncio.create_task(asyncio.sleep(60))
    daemon.tasks["busy"] = active
    try:
        daemon.apply_root_change(
            {"id": "later", "path": str(tmp_path / "new"), "status": "pending"}
        )
        assert daemon.config["workspace_root"] == old
        assert "root_change_result" not in daemon.config
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


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
    # 控制类变量整单拒绝，错误里点名键（不含值），便于管理员定位要删的配置。
    payload = {
        "working_dir": str(root / "hello"),
        "env_vars": {"ANTHROPIC_BASE_URL": "https://secret.example", "LD_PRELOAD": "/x.so"},
    }
    with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL, LD_PRELOAD") as excinfo:
        daemon.validate_command(command(payload))
    assert "secret.example" not in str(excinfo.value)
    ok = {"working_dir": str(root / "hello"), "env_vars": {"COREMAN_BOT_KEY": "b", "DB": "1"}}
    assert daemon.validate_command(command(ok))["env_vars"] == ok["env_vars"]


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
    # 原状态栏命令由采集脚本转交，不被替换掉。
    assert "custom" in (tmp_path / ".cache/claude_rate_limits/capture.sh").read_text()
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


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"])
def test_configured_proxy_overrides_the_service_environment(daemon, monkeypatch, key):
    """Everything the CLIs do inherits this environment, so all four variables must agree."""
    from runtime_daemon.daemon import PROXY_ENV_KEYS

    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, "http://198.51.100.7:3128")
    daemon.config["proxy"] = "http://127.0.0.1:18080"
    daemon.apply_proxy()
    assert [os.environ[name] for name in PROXY_ENV_KEYS] == ["http://127.0.0.1:18080"] * 4
    assert daemon.proxy == {"source": "coreman", "url": "http://127.0.0.1:18080", "pending": False}


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"])
def test_without_configured_proxy_the_environment_is_kept_and_reported(daemon, monkeypatch, key):
    from runtime_daemon.daemon import PROXY_ENV_KEYS

    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, " http://198.51.100.7:3128 ")
    daemon.apply_proxy()
    assert daemon.proxy == {
        "source": "environment",
        "url": "http://198.51.100.7:3128",
        "pending": False,
    }
    # An inherited proxy is reported, never rewritten: the other variables stay as they were.
    assert [name for name in PROXY_ENV_KEYS if name in os.environ] == [key]


def test_no_proxy_anywhere_is_reported_as_none(daemon, monkeypatch):
    from runtime_daemon.daemon import PROXY_ENV_KEYS

    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    daemon.config["proxy"] = "   "
    daemon.apply_proxy()
    assert daemon.proxy == {"source": "none", "url": "", "pending": False}


def test_proxy_edited_in_config_is_pending_until_the_daemon_restarts(daemon, monkeypatch):
    from runtime_daemon.daemon import PROXY_ENV_KEYS

    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    daemon.config["proxy"] = "http://127.0.0.1:18080"
    daemon.apply_proxy()
    daemon.save()
    assert daemon.proxy_state()["pending"] is False
    edited = {**json.loads(daemon.config_path.read_text()), "proxy": "http://127.0.0.1:18081"}
    daemon.config_path.write_text(json.dumps(edited))
    assert daemon.proxy_state() == {
        "source": "coreman",
        "url": "http://127.0.0.1:18080",
        "pending": True,
    }
    # An unreadable config.json only costs the pending flag, never the heartbeat.
    daemon.config_path.write_text("{ not json")
    assert daemon.proxy_state()["pending"] is False


@pytest.mark.parametrize(
    "url,shown",
    [
        ("http://user:s3cret@proxy.example:3128", "http://proxy.example:3128"),
        ("http://p%40ss:w@rd@[::1]:8080/", "http://[::1]:8080/"),
        ("proxy.example:3128", "proxy.example:3128"),
        ("http://proxy.example:3128", "http://proxy.example:3128"),
    ],
)
def test_reported_proxy_never_carries_credentials(daemon, monkeypatch, url, shown):
    """The console shows where traffic goes; the password stays on the node."""
    from runtime_daemon.daemon import PROXY_ENV_KEYS

    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", url)
    daemon.apply_proxy()
    assert daemon.proxy["url"] == shown
    assert os.environ["https_proxy"] == url
    daemon.config["proxy"] = url
    daemon.apply_proxy()
    assert daemon.proxy["url"] == shown
    assert os.environ["HTTPS_PROXY"] == url
    # Redaction is display-only: the pending check still compares the real values.
    daemon.save()
    assert daemon.proxy_state()["pending"] is False
