"""Claude 后台阶段会话在守护进程这一侧：/live/* 的放行与转发、槽位、旧驱动回收、心跳与状态。"""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

import runtime_daemon.daemon as module
from runtime_daemon.agent import Agent
from tests.unit.runtime_daemon_support import make_daemon


def sleeping_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


@pytest.fixture
def daemon(tmp_path):
    instance = make_daemon(tmp_path, max_concurrent=4)
    instance.socket_dir = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    root = Path(instance.config["workspace_root"])
    for provider in ("claude", "codex"):
        instance.agents[provider] = Agent(
            root=root, api_url="http://localhost", token="x" * 32, relay_id=provider, home=tmp_path
        )
    yield instance
    for process in [*instance.drivers.values(), *(p for _, _, p in instance.retired)]:
        if process.poll() is None:
            process.kill()
            process.wait()
    shutil.rmtree(instance.socket_dir, ignore_errors=True)


def command(body, path, method="POST", provider="claude", identity="call-1"):
    return {
        "id": identity,
        "provider": provider,
        "method": method,
        "path": path,
        "body": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else "",
    }


class FakeDrivers:
    """按 socket 文件名应答的假驱动：记录收到的请求，/live/list 返回各代自己的会话。"""

    def __init__(self, monkeypatch, lists=None):
        self.lists = lists or {}
        self.requests: list[tuple[str, str, str, dict]] = []
        real = httpx.AsyncHTTPTransport

        def transport(*, uds=None, **kwargs):
            if uds is None:
                return real(**kwargs)
            name = Path(uds).name
            return httpx.MockTransport(lambda request: self.handle(name, request))

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)

    def handle(self, socket_name, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((socket_name, request.method, request.url.path, body))
        if request.url.path == "/live/list":
            reply = self.lists.get(socket_name, 404)
            if isinstance(reply, Exception):
                raise reply
            if reply == 404:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"sessions": reply})
        if request.url.path == "/live/attach":
            stream = b": keepalive\n\n" + b'data: {"x_runtime":{"turn_end":{"merged":false}}}\n\n'
            return httpx.Response(
                200, content=stream, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json={"ok": True})


async def run(daemon, cmd):
    sent = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(cmd)
    assert sent and sent[-1]["done"] is True
    return sent


def body_of(frames):
    return b"".join(base64.b64decode(frame["data"]) for frame in frames)


def session(sid, tasks=()):
    return {"session_id": sid, "owner": "u1", "deadline": 0, "busy": False, "tasks": list(tasks)}


# ---- 放行与校验 ----


@pytest.mark.parametrize(
    "path,body,expected",
    [
        ("/live/attach", {"session_id": "s-1", "extra": 1}, {"session_id": "s-1"}),
        (
            "/live/stop_task",
            {"session_id": "s-1", "task_id": "a01d89e8"},
            {"session_id": "s-1", "task_id": "a01d89e8"},
        ),
        (
            "/live/close",
            {"session_id": "s-1", "reason": "expired"},
            {"session_id": "s-1", "reason": "expired"},
        ),
    ],
)
def test_live_paths_are_allowed_for_claude(daemon, path, body, expected):
    assert daemon.validate_command(command(body, path)) == expected


def test_live_list_takes_no_body(daemon):
    assert daemon.validate_command(command(None, "/live/list", method="GET")) == {}


@pytest.mark.parametrize(
    "path,body,message",
    [
        ("/live/attach", {}, "会话标识无效"),
        ("/live/attach", {"session_id": "../x"}, "会话标识无效"),
        ("/live/attach", {"session_id": "s" * 201}, "会话标识无效"),
        ("/live/stop_task", {"session_id": "s-1"}, "后台任务标识无效"),
        ("/live/stop_task", {"session_id": "s-1", "task_id": "t" * 101}, "后台任务标识无效"),
        ("/live/stop_task", {"session_id": "s-1", "task_id": "a b"}, "后台任务标识无效"),
        ("/live/close", {"session_id": "s-1"}, "结束原因无效"),
        ("/live/close", {"session_id": "s-1", "reason": "other"}, "结束原因无效"),
        ("/live/attach", ["s-1"], "请求体必须是对象"),
    ],
)
def test_live_bodies_are_validated(daemon, path, body, message):
    with pytest.raises(ValueError, match=message):
        daemon.validate_command(command(body, path))


@pytest.mark.parametrize(
    "method,path", [("POST", "/live/attach"), ("GET", "/live/list"), ("GET", "/live/attach")]
)
def test_live_paths_are_refused_for_other_providers_and_methods(daemon, method, path):
    provider = "claude" if method == "GET" and path == "/live/attach" else "codex"
    body = None if method == "GET" else {"session_id": "s-1"}
    with pytest.raises(ValueError, match="请求路径不支持"):
        daemon.validate_command(command(body, path, method=method, provider=provider))


def test_live_join_chat_needs_no_env_or_working_dir(daemon):
    data = daemon.validate_command(
        command(
            {"session_id": "s-1", "live_join": True, "messages": [], "worktree": True},
            "/v1/chat/completions",
        )
    )
    assert data["live_join"] is True and "working_dir" not in data and "worktree" not in data


def test_live_join_chat_still_checks_what_it_does_carry(daemon):
    root = Path(daemon.config["workspace_root"])
    ok = {"session_id": "s-1", "live_join": True, "working_dir": str(root / "bot")}
    assert daemon.validate_command(command(ok, "/v1/chat/completions"))["working_dir"] == str(
        root / "bot"
    )
    for body, provider in [
        ({"session_id": "s-1", "live_join": "yes"}, "claude"),
        ({"live_join": True}, "claude"),
        ({"session_id": "../x", "live_join": True}, "claude"),
        ({"session_id": "s-1", "live_join": True}, "codex"),
        ({"session_id": "s-1", "live_join": True, "env_vars": {"LD_PRELOAD": "x"}}, "claude"),
    ]:
        with pytest.raises(ValueError):
            daemon.validate_command(command(body, "/v1/chat/completions", provider=provider))


# ---- 转发 ----


async def test_attach_streams_from_the_generation_that_holds_the_session(daemon, monkeypatch):
    drivers = FakeDrivers(
        monkeypatch, {"claude.sock": [session("old-1")], "claude-1.sock": [session("new-1")]}
    )
    daemon.retired.append(("claude", 0, sleeping_process()))
    daemon.driver_gen["claude"] = 1
    daemon.drivers["claude"] = sleeping_process()
    frames = await run(daemon, command({"session_id": "old-1"}, "/live/attach"))
    assert frames[0]["status_code"] == 200
    assert frames[0]["content_type"] == "text/event-stream"
    assert b"turn_end" in body_of(frames) and b": keepalive" in body_of(frames)
    forwarded = [r for r in drivers.requests if r[2] == "/live/attach"]
    assert forwarded == [("claude.sock", "POST", "/live/attach", {"session_id": "old-1"})]
    # attach 不算在途对话，也不占槽位。
    assert daemon.task_info == {} and daemon.live_calls == set()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/live/stop_task", {"session_id": "s-1", "task_id": "t1"}),
        ("/live/close", {"session_id": "s-1", "reason": "user"}),
    ],
)
async def test_control_calls_are_forwarded_to_the_current_driver(daemon, monkeypatch, path, body):
    drivers = FakeDrivers(monkeypatch)
    frames = await run(daemon, command(body, path))
    assert json.loads(body_of(frames)) == {"ok": True}
    assert drivers.requests == [("claude.sock", "POST", path, body)]


async def test_live_list_merges_every_running_generation(daemon, monkeypatch):
    FakeDrivers(
        monkeypatch, {"claude.sock": [session("old-1")], "claude-1.sock": [session("new-1")]}
    )
    daemon.retired.append(("claude", 0, sleeping_process()))
    daemon.driver_gen["claude"] = 1
    daemon.drivers["claude"] = sleeping_process()
    frames = await run(daemon, command(None, "/live/list", method="GET"))
    ids = [s["session_id"] for s in json.loads(body_of(frames))["sessions"]]
    assert ids == ["old-1", "new-1"]


async def test_live_join_chat_goes_to_the_old_driver_and_takes_no_slot(daemon, monkeypatch):
    drivers = FakeDrivers(monkeypatch, {"claude.sock": [session("old-1")], "claude-1.sock": []})
    daemon.retired.append(("claude", 0, sleeping_process()))
    daemon.driver_gen["claude"] = 1
    daemon.drivers["claude"] = sleeping_process()
    seen = {}
    original = daemon.driver_gen_for

    async def spy(provider, sid):
        gen = await original(provider, sid)
        seen["slots"] = daemon.occupied_slots()
        seen["live_call"] = "call-1" in daemon.live_calls
        return gen

    monkeypatch.setattr(daemon, "driver_gen_for", spy)
    daemon.tasks["call-1"] = asyncio.current_task()
    try:
        await run(
            daemon,
            command(
                {"session_id": "old-1", "live_join": True, "stream": True}, "/v1/chat/completions"
            ),
        )
    finally:
        daemon.tasks.clear()
    chats = [r for r in drivers.requests if r[2] == "/v1/chat/completions"]
    assert [r[0] for r in chats] == ["claude.sock"]
    # 这条调用不占槽位，会话本身占一个。
    assert seen == {"slots": 1, "live_call": True}
    # 刚结束一轮的旧驱动：下次读到会话表之前不回收。
    assert 0 in daemon.live_table.call_ended


# ---- 槽位 ----


async def test_live_sessions_take_slots_once_each(daemon):
    daemon.live_table.sessions = {0: [session("a"), session("b")]}
    daemon.live_table.checked = time.monotonic()
    work = {name: asyncio.create_task(asyncio.sleep(60)) for name in ("chat", "attach", "join")}
    daemon.tasks.update(work)
    daemon.live_calls.update({"attach", "join"})
    daemon.task_info["join"] = {"provider": "claude", "session_id": "a"}
    sent = []

    async def api(path, body, *, retry=False, budget=None):
        sent.append(body)
        return {"commands": [], "cancelled": [], "protocol": 2}

    daemon.api = api
    try:
        # 4 个槽位：普通对话 1 + 后台会话 a、b 各 1；attach 与 live_join 不占。
        await daemon.poll_once()
        assert sent[-1]["slots"] == 1
        assert set(sent[-1]["running"]) == {"chat", "attach", "join"}
        assert daemon.heartbeat_body()["active_calls"] == 3
        # 普通对话续的正好是后台会话 b：不重复计。
        daemon.task_info["chat"] = {"provider": "claude", "session_id": "b"}
        await daemon.poll_once()
        assert sent[-1]["slots"] == 2
    finally:
        for task in work.values():
            task.cancel()
        await asyncio.gather(*work.values(), return_exceptions=True)


async def test_live_calls_are_marked_when_claimed(daemon):
    daemon.live_table.checked = time.monotonic()
    started = asyncio.Event()

    async def execute(cmd):
        started.set()
        await asyncio.sleep(60)

    async def api(path, body, *, retry=False, budget=None):
        return {
            "commands": [
                {"id": "attach-1", "provider": "claude", "method": "POST", "path": "/live/attach"},
                {"id": "chat-1", "provider": "claude", "method": "POST", "path": "/"},
            ],
            "cancelled": [],
            "protocol": 2,
        }

    daemon.api, daemon.execute = api, execute
    await daemon.poll_once()
    try:
        assert daemon.live_calls == {"attach-1"} and daemon.occupied_slots() == 1
    finally:
        await daemon.cancel_all()


async def test_ended_sessions_free_slots(daemon, monkeypatch):
    drivers = FakeDrivers(monkeypatch, {"claude.sock": [session("a")]})
    daemon.drivers["claude"] = sleeping_process()
    await daemon.refresh_live(force=True)
    assert daemon.live_table.ids() == ["a"] and daemon.occupied_slots() == 1
    daemon.slots_freed.clear()
    drivers.lists["claude.sock"] = []
    await daemon.refresh_live()  # 缓存未过期：不再问驱动
    assert daemon.live_table.ids() == ["a"] and len(drivers.requests) == 1
    await daemon.refresh_live(force=True)
    assert daemon.live_table.ids() == [] and daemon.slots_freed.is_set()


async def test_unreachable_driver_counts_as_no_sessions_and_logs_once(daemon, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="coreman-runtime")
    FakeDrivers(monkeypatch, {"claude.sock": httpx.ConnectError("gone")})
    daemon.drivers["claude"] = sleeping_process()
    for _ in range(4):
        await daemon.refresh_live(force=True)
    assert daemon.live_table.ids() == []
    assert len([r for r in caplog.records if "live list" in r.getMessage()]) == 1


async def test_driver_without_live_list_and_stopped_driver_have_no_sessions(daemon, monkeypatch):
    FakeDrivers(monkeypatch)  # 旧驱动：/live/list 404
    daemon.drivers["claude"] = sleeping_process()
    await daemon.refresh_live(force=True)
    assert daemon.live_table.sessions == {0: []}
    daemon.live_table.sessions = {0: [session("a")]}
    daemon.drivers["claude"].kill()
    daemon.drivers["claude"].wait()
    await daemon.refresh_live(force=True)
    assert daemon.live_table.ids() == []


# ---- 旧驱动回收 ----


async def test_rotation_keeps_a_driver_with_live_sessions(daemon, monkeypatch):
    drivers = FakeDrivers(monkeypatch, {"claude.sock": [session("old-1")], "claude-1.sock": []})
    old = sleeping_process()
    daemon.retired.append(("claude", 0, old))
    daemon.driver_gen["claude"] = 1
    daemon.drivers["claude"] = sleeping_process()
    await daemon.refresh_live(force=True)
    assert daemon.retired and old.poll() is None
    # 会话结束了，但一轮对话在上次读表之后才结束：读到新的会话表之前不回收。
    daemon.live_table.sessions[0] = []
    daemon.live_table.call_ended[0] = time.monotonic()
    daemon.reap_retired()
    assert daemon.retired and old.poll() is None
    drivers.lists["claude.sock"] = []
    await daemon.refresh_live(force=True)
    assert daemon.retired == []
    old.wait(timeout=10)
    assert 0 not in daemon.live_table.call_ended


# ---- 心跳与状态 ----


async def test_heartbeat_reports_live_sessions(daemon, monkeypatch):
    FakeDrivers(monkeypatch, {"claude.sock": [session("a")], "claude-1.sock": [session("b")]})
    daemon.retired.append(("claude", 0, sleeping_process()))
    daemon.driver_gen["claude"] = 1
    daemon.drivers["claude"] = sleeping_process()
    sent = []

    async def api(path, body, *, retry=False, budget=None):
        sent.append(body)
        daemon.stopping.set()
        return {}

    daemon.api = api
    await daemon.heartbeat_loop()
    assert sent[0]["live_sessions"] == ["a", "b"] and sent[0]["active_calls"] == 2


def test_heartbeat_without_live_sessions(daemon):
    body = daemon.heartbeat_body()
    assert body["live_sessions"] == [] and body["active_calls"] == 0


def test_status_counts_live_sessions_as_active_work(daemon):
    daemon.config["node_token"] = "t" * 32
    claude = module.RuntimeAgent(daemon, "claude", "relay-claude")
    codex = module.RuntimeAgent(daemon, "codex", "relay-codex")
    try:
        idle = claude.dispatch({"type": "status"})
        assert idle["active"] is False and idle["count"] == 0 and idle["live_count"] == 0
        daemon.live_table.sessions = {0: [session("a"), session("b")]}
        busy = claude.dispatch({"type": "status"})
        assert busy["active"] is True and busy["count"] == 2
        assert busy["live_sessions"] == ["a", "b"] and busy["active_tasks"] == []
        assert codex.dispatch({"type": "status"})["live_count"] == 0
    finally:
        claude.stop.set()
        codex.stop.set()
