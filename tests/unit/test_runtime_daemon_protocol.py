"""Daemon side of node protocol 2: frame batching, long polling and the local state file."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from runtime_daemon import daemon as module
from runtime_daemon.daemon import Daemon, FrameBatcher


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "workspace_root": str(root),
                "api_url": "http://localhost",
                "node_id": "node-1",
                "max_concurrent": 2,
            }
        )
    )
    return Daemon(path)


def payload(frames):
    return b"".join(base64.b64decode(frame["data"]) for frame in frames)


def collector():
    sent = []

    async def send(frames):
        sent.append(frames)

    return sent, send


async def test_batcher_coalesces_small_parts_and_merges_the_header():
    sent, send = collector()
    batcher = FrameBatcher(send)
    await batcher.add(status_code=200, content_type="text/event-stream")
    for part in (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"):
        await batcher.add(part)
    assert sent == []
    await asyncio.sleep(0.2)
    assert len(sent) == 1 and len(sent[0]) == 1
    assert sent[0][0]["seq"] == 0 and sent[0][0]["status_code"] == 200
    assert payload(sent[0]) == b"data: a\n\ndata: b\n\ndata: c\n\n"
    await batcher.finish()
    assert sent[-1] == [{"seq": 1, "data": "", "done": True}]


async def test_batcher_flushes_at_16_kib_and_splits_into_chunk_sized_frames():
    sent, send = collector()
    batcher = FrameBatcher(send)
    await batcher.add(b"x" * (16 * 1024))
    assert len(sent) == 1
    await batcher.add(b"y" * (100 * 1024))
    sizes = [len(base64.b64decode(frame["data"])) for frame in sent[1]]
    assert sizes == [module.CHUNK_SIZE, module.CHUNK_SIZE, 100 * 1024 - 2 * module.CHUNK_SIZE]
    assert [frame["seq"] for frame in sent[1]] == [1, 2, 3]
    await batcher.finish(error="execution_failed")
    assert sent[-1][-1] == {"seq": 4, "data": "", "done": True, "error": "execution_failed"}


async def test_batcher_resends_frames_whose_send_failed():
    calls = []

    async def send(frames):
        calls.append([frame["seq"] for frame in frames])
        if len(calls) == 1:
            raise httpx.ConnectError("control plane unreachable")

    batcher = FrameBatcher(send)
    await batcher.add(b"a")
    await asyncio.sleep(0.2)
    with pytest.raises(httpx.ConnectError):
        await batcher.add(b"b")
    await batcher.finish()
    assert calls == [[0], [0, 1]]


async def test_send_frames_falls_back_to_single_frames_for_an_older_api(node):
    posts = []

    async def api(path, body, *, retry=False, budget=None):
        posts.append(body)
        if "frames" in body:
            request = httpx.Request("POST", "http://localhost" + path)
            raise httpx.HTTPStatusError(
                "old api", request=request, response=httpx.Response(422, request=request)
            )
        return {}

    node.api = api
    node.server_protocol = 2
    frames = [{"seq": 0, "data": ""}, {"seq": 1, "data": "", "done": True}]
    await node.send_frames("call", frames)
    assert posts == [{"frames": frames}, *frames] and node.server_protocol == 1
    posts.clear()
    await node.send_frames("call", frames)
    assert posts == frames


def test_state_file_is_written_on_change_and_otherwise_throttled(node, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    state = node.data_dir / "state.json"
    node.write_state(True)
    written = json.loads(state.read_text())
    assert written["online"] is True and written["node_id"] == "node-1"
    state.unlink()
    clock[0] += 5
    node.write_state(True)
    assert not state.exists()
    node.write_state(False)
    assert json.loads(state.read_text())["online"] is False
    state.unlink()
    clock[0] += module.STATE_WRITE_SECONDS + 1
    node.write_state(False)
    assert state.exists()


async def test_full_node_drops_its_wait_when_a_slot_frees(node):
    node.config["max_concurrent"] = 1
    started = asyncio.Event()

    async def api(path, body, *, retry=False, budget=None):
        assert body["slots"] == 0 and body["wait"] == module.POLL_WAIT_SECONDS
        started.set()
        await asyncio.sleep(60)

    node.api = api
    busy = asyncio.create_task(asyncio.sleep(60))
    node.tasks["one"] = busy
    polling = asyncio.create_task(node.poll_once())
    try:
        await asyncio.wait_for(started.wait(), 1)
        node.slots_freed.set()
        assert await asyncio.wait_for(polling, 1) is None
    finally:
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)


async def test_stopping_mid_poll_hands_claimed_commands_back(node):
    requests = []
    release = asyncio.Event()

    async def api(path, body, *, retry=False, budget=None):
        requests.append(body)
        if len(requests) == 1:
            await release.wait()
            command = {"id": "c1", "provider": "claude"}
            return {"commands": [command], "cancelled": [], "protocol": 2}
        return {"commands": [], "cancelled": [], "protocol": 2}

    node.api = api
    polling = asyncio.create_task(node.poll_once())
    await asyncio.sleep(0.05)
    node.stopping.set()
    await asyncio.sleep(0.05)
    assert not polling.done()  # a round with free slots may already have claimed work
    release.set()
    result = await asyncio.wait_for(polling, 1)
    assert result["commands"][0]["id"] == "c1" and not node.tasks
    assert requests[-1] == {"abandoned": ["c1"], "slots": 0}
    assert json.loads((node.data_dir / "state.json").read_text())["online"] is True
    assert node.server_protocol == 2


async def test_heartbeat_reports_protocol_and_concurrency(node):
    body = node.heartbeat_body()
    assert body["protocol"] == module.PROTOCOL_VERSION == 2
    assert {"claude", "codex", "version", "service_status"} <= set(body)
    assert body["max_concurrent"] == 2 and body["active_calls"] == 0
    running = asyncio.create_task(asyncio.sleep(60))
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    node.tasks.update(one=running, two=finished)
    try:
        assert node.heartbeat_body()["active_calls"] == 1
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_heartbeat_reports_the_git_hosts_the_agent_enforces(node):
    node.config["node_token"] = "synthetic-node-token-" + "x" * 30
    assert node.heartbeat_body()["git_hosts"] == ["github.com"]
    node.config["git_hosts"] = ["github.com", "git.corp.example"]
    agent = module.RuntimeAgent(node, "claude", "relay-1")
    assert node.heartbeat_body()["git_hosts"] == ["github.com", "git.corp.example"]
    assert agent.git_hosts == set(node.heartbeat_body()["git_hosts"])
