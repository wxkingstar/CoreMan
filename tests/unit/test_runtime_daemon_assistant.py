"""守护进程接上个人助理的轮次之后：普通轮次一字不变，带 sandbox 的才分出去，能力照自检上报。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import agent as agent_module
from runtime_daemon import daemon as module
from runtime_daemon.daemon import Daemon


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "projects"
    (root / "bot-1").mkdir(parents=True)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"workspace_root": str(root), "api_url": "http://localhost", "node_id": "n1"})
    )
    daemon = Daemon(path)
    daemon.socket_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    daemon.agents["claude"] = agent_module.Agent(
        root=root,
        home=tmp_path / "home",
        api_url="http://localhost",
        token="synthetic-test-token-123456789",
        relay_id="relay-1",
        git_hosts=("github.com",),
    )
    return daemon


async def run(daemon: Daemon, body: dict) -> list[dict]:
    sent: list[dict] = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        }
    )
    return sent


async def fake_driver(path: Path, received: list[dict]) -> asyncio.AbstractServer:
    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        received.append({"request_line": lines[0].decode()})
        length = next(
            int(line.split(b":")[1]) for line in lines if line.lower().startswith(b"content-length")
        )
        received.append(json.loads(await reader.readexactly(length)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


class Untouchable:
    """换掉 node.assistant：普通轮次碰它的任何属性（含 run）都算失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"standard turns must not touch the assistant path ({name})")


async def test_standard_turn_reaches_the_resident_driver_unchanged(node):
    node.assistant = Untouchable()
    received: list[dict] = []
    server = await fake_driver(node.socket_path("claude"), received)
    body = {
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "working_dir": "bot-1",
        "session_id": "s1",
        "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
    }
    try:
        frames = await run(node, body)
    finally:
        server.close()
    assert frames[-1]["done"] is True and not frames[-1].get("error")
    assert received[0] == {"request_line": "POST /v1/chat/completions HTTP/1.1"}
    forwarded = received[1]
    # 与接入助理之前一样：工作目录换成绝对路径、记忆目录写进 settings，其余字段原样。
    assert forwarded["working_dir"] == str(Path(node.config["workspace_root"]) / "bot-1")
    assert set(forwarded) == {*body, "settings"}
    assert {k: forwarded[k] for k in body if k != "working_dir"} == {
        k: v for k, v in body.items() if k != "working_dir"
    }
    assert "autoMemoryDirectory" in json.loads(forwarded["settings"])
    # 回给平台的帧：状态、席位帧、原样转发的流，没有别的。
    payload = b"".join(base64.b64decode(f["data"]) for f in frames)
    assert payload.startswith(b'data: {"x_runtime": {"seat_id": ')
    assert payload.endswith(b"data: [DONE]\n\n")
    assert frames[0]["status_code"] == 200
    assert node.task_info == {}


async def test_sandbox_requests_go_to_the_assistant_path_only(node, monkeypatch):
    calls: list[tuple] = []

    async def fake_run(identity, provider, data, frames):
        calls.append((identity, provider, data))
        await frames.finish()

    async def no_memory(*_args):
        raise AssertionError("assistant turns skip the shared memory layout")

    monkeypatch.setattr(node.assistant, "run", fake_run)
    monkeypatch.setattr(node, "attach_memory", no_memory)
    frames = await run(
        node,
        {
            "sandbox": True,
            "transcript": {"transfer": "0" * 32, "restore": False},
            "messages": [{"role": "user", "content": "hi"}],
            "working_dir": "bot-1",
        },
    )
    assert calls and calls[0][2]["sandbox"] is True
    assert calls[0][2]["working_dir"] == str(Path(node.config["workspace_root"]) / "bot-1")
    assert frames[-1]["done"] is True
    assert node.task_info == {}


async def test_assistant_failures_end_only_that_call(node, monkeypatch):
    async def broken(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(node.assistant, "run", broken)
    frames = await run(node, {"sandbox": True, "messages": [], "working_dir": "bot-1"})
    assert frames[-1]["error"] == "execution_failed"
    assert node.task_info == {}


async def test_capability_follows_the_self_test(node, monkeypatch):
    monkeypatch.setattr(module, "cli_status", lambda provider, env=None: {"installed": False})
    node.assistant_only = True
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is False
    node.assistant.ready["claude"] = True
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is True
    assert node.capabilities["opencode"]["assistant_sandbox_v1"] is False
    assert node.heartbeat_body()["claude"]["assistant_sandbox_v1"] is True


async def test_discovery_survives_a_broken_assistant(node, monkeypatch):
    monkeypatch.setattr(module, "cli_status", lambda provider, env=None: {"installed": False})

    def broken(_provider):
        raise RuntimeError("boom")

    monkeypatch.setattr(node.assistant, "capability", broken)
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is False
    assert node.capabilities["claude"]["memory_layout_v1"] is True


async def test_background_work_never_raises(node, monkeypatch):
    async def explode():
        raise OSError("disk")

    await node.assistant._guard("sweep", explode)


def test_heartbeat_names_the_machine_by_hash_and_the_system_user(node, tmp_path):
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("0123abcd\n")
    fingerprint = module.machine_fingerprint((str(tmp_path / "missing"), str(machine_id)))
    assert len(fingerprint) == 64 and "0123abcd" not in fingerprint
    assert fingerprint == module.machine_fingerprint((str(machine_id),))
    assert module.machine_fingerprint((str(tmp_path / "missing"),)) == ""
    body = node.heartbeat_body()
    assert body["uid"] == os.getuid() and body["machine"] == node.machine


def test_unreadable_machine_id_is_not_fatal(tmp_path):
    garbage = tmp_path / "machine-id"
    garbage.write_bytes(b"\xff\xfe\x00")
    assert module.machine_fingerprint((str(garbage),)) == ""
    assert module.machine_fingerprint((str(tmp_path),)) == ""


def test_a_failing_fingerprint_does_not_stop_the_daemon(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "machine_fingerprint", broken)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "node_id": "n1"}))
    assert Daemon(path).machine == ""


async def test_assistant_startup_and_shutdown_failures_are_contained(node, monkeypatch):
    def broken_start():
        raise RuntimeError("boom")

    async def broken_stop():
        raise RuntimeError("boom")

    monkeypatch.setattr(node.assistant, "start", broken_start)
    monkeypatch.setattr(node.assistant, "stop", broken_stop)
    node.assistant_only = True
    node.start_assistant()
    await node.stop_assistant()


def test_self_test_requires_home_data_and_workspace_to_be_private(node):
    paths = node.assistant.private_paths()
    assert node.data_dir in paths
    assert Path(node.config["workspace_root"]) in paths


# ---- 助理专用实例（assistant_only） ---------------------------------------------


def test_assistant_only_is_read_from_the_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "node_id": "n1"}))
    assert Daemon(path).assistant_only is False
    for value in ("true", 1, "yes"):
        path.write_text(
            json.dumps({"workspace_root": str(tmp_path), "node_id": "n1", "assistant_only": value})
        )
        assert Daemon(path).assistant_only is False
    path.write_text(
        json.dumps({"workspace_root": str(tmp_path), "node_id": "n1", "assistant_only": True})
    )
    assert Daemon(path).assistant_only is True


async def test_an_ordinary_daemon_never_advertises_the_sandbox(node, monkeypatch):
    monkeypatch.setattr(module, "cli_status", lambda provider, env=None: {"installed": False})
    node.assistant.ready["claude"] = True
    await node.discover()
    assert node.capabilities["claude"]["assistant_sandbox_v1"] is False
    assert node.assistant.capability("claude") is False


def test_an_ordinary_daemon_runs_no_assistant_background_work(node, monkeypatch):
    def started():
        raise AssertionError("an ordinary daemon runs no sandbox self-test or sweep")

    monkeypatch.setattr(node.assistant, "start", started)
    node.start_assistant()


def platform_client(uploads: list) -> object:
    import httpx

    def handler(request):
        uploads.append((request.method, request.headers.get("X-Transcript-Status")))
        return httpx.Response(200, json={"code": 0})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://coreman.example.com"
    )


async def test_a_sandbox_request_to_an_ordinary_daemon_is_refused(node, monkeypatch):
    """普通实例即便自检通过也不跑助理：带 sandbox 的请求拒绝，不碰任何驱动。"""
    uploads: list = []
    node.http = platform_client(uploads)
    node.assistant.ready["claude"] = True

    def no_launch(*_a, **_k):
        raise AssertionError("must not start a sandbox")

    monkeypatch.setattr(node.assistant, "_launch", no_launch)
    frames = await run(
        node,
        {
            "sandbox": True,
            "transcript": {"transfer": "0" * 32, "restore": False},
            "messages": [{"role": "user", "content": "hi"}],
            "working_dir": "bot-1",
        },
    )
    assert frames[0]["status_code"] == 503
    body = json.loads(base64.b64decode(frames[0]["data"]))
    assert body["error"]["code"] == "sandbox_unavailable"
    assert uploads == [("PUT", "unchanged")]
    assert frames[-1]["done"] is True


async def execute(daemon: Daemon, method: str, path: str, body: dict | None) -> list[dict]:
    sent: list[dict] = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    raw = json.dumps(body).encode() if body is not None else b""
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": method,
            "path": path,
            "body": base64.b64encode(raw).decode(),
        }
    )
    return sent


def refusal_code(frames: list[dict]) -> str:
    assert frames[0]["status_code"] == 403
    assert frames[-1]["done"] is True and not frames[-1].get("error")
    return json.loads(base64.b64decode(frames[0]["data"]))["error"]["code"]


@pytest.mark.parametrize(
    "extra",
    [{}, {"sandbox": False}, {"sandbox": "true"}, {"sandbox": 1}, {"sandbox": None}],
)
async def test_assistant_only_refuses_chat_without_a_sandbox(node, monkeypatch, extra):
    node.assistant_only = True
    node.assistant = Untouchable()

    async def no_memory(*_args):
        raise AssertionError("refused before doing anything")

    monkeypatch.setattr(node, "attach_memory", no_memory)
    body = {"messages": [{"role": "user", "content": "hi"}], "working_dir": "new-bot", **extra}
    frames = await execute(node, "POST", "/v1/chat/completions", body)
    assert refusal_code(frames) == "assistant_only"
    # 拒绝发生在一切动作之前：连工作目录都没有建。
    assert not (Path(node.config["workspace_root"]) / "new-bot").exists()
    assert node.task_info == {}


async def test_assistant_only_sends_sandboxed_chat_to_the_assistant_path(node, monkeypatch):
    node.assistant_only = True
    calls: list = []

    async def fake_run(identity, provider, data, frames):
        calls.append(data)
        await frames.finish()

    monkeypatch.setattr(node.assistant, "run", fake_run)
    body = {
        "sandbox": True,
        "transcript": {"transfer": "0" * 32, "restore": False},
        "messages": [],
        "working_dir": "bot-1",
    }
    await execute(node, "POST", "/v1/chat/completions", body)
    assert calls and calls[0]["sandbox"] is True


REFUSED_OPERATIONS = [
    "install-skill",
    "workspace-list",
    "workspace-read",
    "workspace-write",
    "workspace-git-status",
    "workspace-git-diff",
    "workspace-export",
    "workspace-import",
    "workspace-finish",
    "workspace-clone",
    "session-export",
    "session-read",
    "session-import",
    "read-memory",
    "deploy-memory",
    "collect-memory",
    "refresh-worktrees",
    "mcp-login-start",
    "mcp-login-complete",
    "mcp-logout",
    "pull",
    "pr",
    "init",
    "anything-new",
    None,
]


@pytest.mark.parametrize("kind", REFUSED_OPERATIONS)
async def test_assistant_only_refuses_operations_off_the_allow_list(node, monkeypatch, kind):
    node.assistant_only = True

    def no_dispatch(data):
        raise AssertionError("refused operations never reach the agent")

    monkeypatch.setattr(node.agents["claude"], "dispatch", no_dispatch)
    frames = await execute(node, "POST", "/", {"type": kind, "working_dir": "bot-1"})
    assert refusal_code(frames) == "assistant_only"
    assert node.task_info == {}


ALLOWED_OPERATIONS = [
    "ping",
    "status",
    "health-check",
    "probe-rate-limits",
    "workspace-info",
    "workspace-probe",
    "workspace-init",
    "workspace-remove",
    "seat-create",
    "seat-login-start",
    "seat-login-complete",
    "seat-activate",
    "seat-remove",
]


def test_the_allow_list_is_exactly_what_the_platform_sends_an_assistant_node():
    assert module.ASSISTANT_ONLY_OPERATIONS == frozenset(ALLOWED_OPERATIONS)
    assert agent_module.SEAT_OPERATIONS <= module.ASSISTANT_ONLY_OPERATIONS


@pytest.mark.parametrize("kind", ALLOWED_OPERATIONS)
async def test_assistant_only_runs_operations_on_the_allow_list(node, monkeypatch, kind):
    node.assistant_only = True
    seen: list = []

    def dispatch(data):
        seen.append(data["type"])
        return {"success": True}

    monkeypatch.setattr(node.agents["claude"], "dispatch", dispatch)
    frames = await execute(node, "POST", "/", {"type": kind})
    assert seen == [kind]
    assert frames[0]["status_code"] == 200


@pytest.mark.parametrize("path", ["/v1/stats", "/sessions", "/session/s1/events"])
async def test_assistant_only_refuses_driver_reads_other_than_health_and_models(node, path):
    node.assistant_only = True
    frames = await execute(node, "GET", path, None)
    assert refusal_code(frames) == "assistant_only"


@pytest.mark.parametrize("path", ["/health", "/v1/models"])
async def test_assistant_only_keeps_health_and_model_listing(node, path):
    node.assistant_only = True
    received: list[dict] = []

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        received.append({"request_line": head.split(b"\r\n")[0].decode()})
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
        writer.write(b"Content-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(node.socket_path("claude")))
    try:
        frames = await execute(node, "GET", path, None)
    finally:
        server.close()
    assert received == [{"request_line": f"GET {path} HTTP/1.1"}]
    assert frames[0]["status_code"] == 200


def test_assistant_only_agents_skip_the_working_directory_jobs(node, monkeypatch):
    node.config["node_token"] = "synthetic-node-token"
    node.assistant_only = True
    assert module.RuntimeAgent(node, "claude", "relay-1").workspace_jobs is False
    node.assistant_only = False
    assert module.RuntimeAgent(node, "claude", "relay-1").workspace_jobs is True


@pytest.mark.parametrize(("jobs", "expected"), [(True, True), (False, False)])
def test_schedules_walk_working_directories_only_when_allowed(node, monkeypatch, jobs, expected):
    import threading

    monkeypatch.setenv("COREMAN_MEMORY_SYNC", "1")
    agent = node.agents["claude"]
    agent.workspace_jobs = jobs
    started: list[str] = []
    first_round = threading.Event()

    def start_background(name, function):
        started.append(name)
        if name == "health-check":
            first_round.set()
        return {"success": True}

    agent.start_background = start_background
    agent.start_schedules()
    try:
        assert first_round.wait(5)
    finally:
        agent.stop.set()
    assert "health-check" in started
    assert ("collect-memory" in started) is expected
    assert ("refresh-worktrees" in started) is expected


class FakeDriver:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.events.append("terminate")
        self.done = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.done = True


async def test_shutdown_terminates_drivers_without_waiting_for_the_assistant(node, monkeypatch):
    events: list[str] = []
    release = asyncio.Event()

    async def slow_stop():
        events.append("assistant-stop")
        await release.wait()
        events.append("assistant-stopped")

    monkeypatch.setattr(node.assistant, "stop", slow_stop)
    node.drivers["claude"] = FakeDriver(events)  # type: ignore[assignment]
    work = asyncio.ensure_future(node.stop_work())
    for _ in range(100):
        if "terminate" in events:
            break
        await asyncio.sleep(0.01)
    assert "terminate" in events and "assistant-stopped" not in events
    release.set()
    await asyncio.wait_for(work, 2)
    assert events[-1] == "assistant-stopped"


async def test_a_failing_assistant_stop_still_terminates_drivers(node, monkeypatch):
    events: list[str] = []

    async def broken_stop():
        raise RuntimeError("boom")

    monkeypatch.setattr(node.assistant, "stop", broken_stop)
    node.drivers["claude"] = FakeDriver(events)  # type: ignore[assignment]
    await asyncio.wait_for(node.stop_work(), 2)
    assert events == ["terminate"]


# ---- 在跑的轮次登记工作目录：删目录前靠它判断「还在用」 ----------------------------------


async def test_a_turn_registers_its_workdir_before_launch_and_clears_it_on_every_exit(
    tmp_path, monkeypatch
):
    from runtime_daemon import turn_credentials
    from tests.unit.test_assistant_turns import Frames, Platform, fake_sandbox, make, request

    turns = make(tmp_path, Platform())
    fake_sandbox(turns, monkeypatch)
    workdir = (tmp_path / "projects" / "assistant-1").resolve()
    seen: list = []
    fake_launch = turns._launch

    def launch(*args):
        seen.append(list(turns.workdirs.values()))
        return fake_launch(*args)

    monkeypatch.setattr(turns, "_launch", launch)
    # 正常结束。
    await turns.run("call-1", "claude", request(tmp_path), Frames())
    assert seen == [[workdir]] and turns.workdirs == {}

    # 拒绝（凭证拿不到）。
    def unavailable(*_a):
        seen.append(list(turns.workdirs.values()))
        raise turn_credentials.Unavailable("credential_unavailable")

    original = turn_credentials.for_provider
    monkeypatch.setattr(turn_credentials, "for_provider", unavailable)
    await turns.run("call-2", "claude", request(tmp_path), Frames())
    assert seen[-1] == [workdir] and turns.workdirs == {}
    monkeypatch.setattr(turn_credentials, "for_provider", original)

    # 启动出错。
    def broken(*_a):
        seen.append(list(turns.workdirs.values()))
        raise RuntimeError("launch failed")

    monkeypatch.setattr(turns, "_launch", broken)
    with pytest.raises(RuntimeError):
        await turns.run("call-3", "claude", request(tmp_path), Frames())
    assert seen[-1] == [workdir] and turns.workdirs == {}
