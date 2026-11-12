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
    node.start_assistant()
    await node.stop_assistant()


def test_self_test_requires_home_data_and_workspace_to_be_private(node):
    paths = node.assistant.private_paths()
    assert node.data_dir in paths
    assert Path(node.config["workspace_root"]) in paths
