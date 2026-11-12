"""守护进程上的个人助理轮次：拒绝条件、[DONE] 等转录上传完才放出、清扫与取消。

沙箱进程用替身：`_launch` / `_wait_ready` / `_stop` 换成一个进程内的假驱动（Unix socket 上的
最小 HTTP 服务），它像 CLI 一样把转录写进本轮目录。真起 bubblewrap 的用例见 test_assistant_sandbox。
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from runtime_daemon import assistant_turns, sandbox, transcripts, turn_credentials
from runtime_daemon.assistant_turns import AssistantTurns, DoneGate, plan_turn

TRANSFER = "0" * 31 + "1"
SSE = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'


class Frames:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, dict]] = []

    async def add(self, data: bytes = b"", **meta) -> None:
        self.events.append(("add", data, meta))

    async def finish(self, **final) -> None:
        self.events.append(("finish", b"", final))

    def body(self) -> bytes:
        return b"".join(data for kind, data, _ in self.events if kind == "add")


class Seats:
    def active(self, provider):
        return "default"

    def env(self, provider, seat):
        return {}


class Platform:
    """平台的转录接口替身：记下收到的上传，按设定回下载。"""

    def __init__(self) -> None:
        self.download: tuple[int, bytes] = (404, b"")
        self.upload_status = 200
        self.uploads: list[tuple[str, bytes]] = []
        self.events: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.events.append("download")
            return httpx.Response(self.download[0], content=self.download[1])
        self.events.append("upload")
        self.uploads.append((request.headers["X-Transcript-Status"], request.content))
        return httpx.Response(self.upload_status, json={"code": 0})


def make(tmp_path: Path, platform: Platform) -> AssistantTurns:
    root = tmp_path / "projects"
    (root / "assistant-1").mkdir(parents=True)
    daemon = SimpleNamespace(
        data_dir=tmp_path / "data",
        socket_dir=Path(tempfile.mkdtemp(dir="/tmp")),
        config={
            "api_url": "https://coreman.example.com",
            "workspace_root": str(root),
            "assistant_max_concurrent": 1,
        },
        seats=Seats(),
        task_info={},
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(platform.handler),
            base_url="https://coreman.example.com",
        ),
    )
    daemon.data_dir.mkdir()
    turns = AssistantTurns(daemon)
    turns.ready["claude"] = True
    return turns


def request(tmp_path: Path, restore: bool = False, **extra) -> dict:
    return {
        "sandbox": True,
        "transcript": {"transfer": TRANSFER, "restore": restore},
        "working_dir": str(tmp_path / "projects" / "assistant-1"),
        "session_id": "s1",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "env_vars": {"COREMAN_FEISHU_PERSONAL_TOKEN": "turn-token"},
        **extra,
    }


def fake_sandbox(
    turns: AssistantTurns,
    monkeypatch,
    *,
    seen: list | None = None,
    hang=False,
    stop_gate: tuple[int, asyncio.Event, asyncio.Event] | None = None,
):
    """进程内的假驱动：收到请求后写一份转录，回一段 SSE。

    `stop_gate=(n, entered, release)`：第 n 次 `_stop` 先置 entered，再等 release。
    """
    monkeypatch.setattr(
        turn_credentials,
        "for_provider",
        lambda *_a: turn_credentials.Credential(env={"ANTHROPIC_API_KEY": "k"}),
    )
    monkeypatch.setattr(sandbox, "link_cli", lambda *_a, **_k: True)
    servers: list[asyncio.AbstractServer] = []

    def launch(layout, plan, credential, credential_file):
        return SimpleNamespace(layout=layout, plan=plan)

    async def wait_ready(layout, process):
        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":")[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length")
                )
            )
            body = json.loads(await reader.readexactly(length))
            restored = layout.root / "config/projects/-work/s1.jsonl"
            if seen is not None:
                seen.append((body, restored.exists() and restored.read_bytes()))
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(b'{"turn":"new"}\n')
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            writer.write(SSE[:30])
            await writer.drain()
            if hang:
                await asyncio.sleep(3600)
            writer.write(SSE[30:])
            await writer.drain()
            writer.close()

        servers.append(await asyncio.start_unix_server(handle, path=str(layout.driver_socket)))

    stops = [0]

    async def stop(process):
        stops[0] += 1
        if stop_gate is not None and stops[0] == stop_gate[0]:
            stop_gate[1].set()
            await stop_gate[2].wait()
        for server in servers:
            server.close()

    monkeypatch.setattr(turns, "_launch", launch)
    monkeypatch.setattr(turns, "_wait_ready", wait_ready)
    monkeypatch.setattr(turns, "_stop", stop)


def unpacked(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}  # type: ignore[union-attr]


async def test_turn_streams_and_releases_done_only_after_the_upload(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    frames = Frames()

    original = turns.upload

    async def upload(transfer, data, status):
        # 上传的那一刻，[DONE] 还没有交给平台。
        assert b"[DONE]" not in frames.body()
        return await original(transfer, data, status)

    monkeypatch.setattr(turns, "upload", upload)
    await turns.run("call-1", "claude", request(tmp_path), frames)
    body, _ = seen[0]
    assert body["working_dir"] == "/work"
    assert "sandbox" not in body and "transcript" not in body
    assert "WebSearch" in json.loads(body["settings"])["permissions"]["deny"]
    assert frames.events[0][2]["status_code"] == 200
    assert frames.body().endswith(b"data: [DONE]\n\n")
    assert frames.events[-1][0] == "finish" and frames.events[-1][2] == {}
    status, data = platform.uploads[0]
    assert status == "ok"
    assert unpacked(data) == {"config/projects/-work/s1.jsonl": b'{"turn":"new"}\n'}
    assert not any((turns.turns_dir).iterdir())
    assert not list(turns.daemon.socket_dir.glob("a*"))
    assert turns.active == set() and turns.daemon.task_info == {}


async def test_restore_puts_the_transcript_back_before_the_driver_runs(tmp_path, monkeypatch):
    platform = Platform()
    source = tmp_path / "old"
    (source / "config/projects/-work").mkdir(parents=True)
    (source / "config/projects/-work/s1.jsonl").write_bytes(b'{"turn":"old"}\n')
    platform.download = (200, transcripts.pack(source, "claude", "s1") or b"")
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    await turns.run("call-1", "claude", request(tmp_path, restore=True), Frames())
    assert seen[0][1] == b'{"turn":"old"}\n'
    assert platform.events == ["download", "upload"]


async def test_platform_without_a_transcript_starts_fresh(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    seen: list = []
    fake_sandbox(turns, monkeypatch, seen=seen)
    await turns.run("call-1", "claude", request(tmp_path, restore=True), Frames())
    assert seen[0][1] is False


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda t, m: t.ready.update(claude=False), "sandbox_unavailable"),
        (lambda t, m: t.active.add("other"), "sandbox_busy"),
        (
            lambda t, m: m.setattr(
                turn_credentials,
                "for_provider",
                lambda *_a: (_ for _ in ()).throw(
                    turn_credentials.Unavailable("credential_expiring")
                ),
            ),
            "credential_expiring",
        ),
    ],
)
async def test_refusals_answer_503_with_a_code(tmp_path, monkeypatch, setup, code):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    setup(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    assert frames.events[0][2]["status_code"] == 503
    assert json.loads(frames.events[0][1])["error"]["code"] == code
    assert frames.events[-1][0] == "finish"
    # 转录没动：平台照旧保留上一轮的，下一轮还能续上。
    assert platform.uploads == [("unchanged", b"")]
    assert not turns.turns_dir.exists() or not any(turns.turns_dir.iterdir())


async def test_download_failure_refuses_instead_of_starting_blank(tmp_path, monkeypatch):
    platform = Platform()
    platform.download = (500, b"")
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path, restore=True), frames)
    assert json.loads(frames.events[0][1])["error"]["code"] == "transcript_unavailable"
    assert platform.uploads == [("unchanged", b"")]
    assert not any(turns.turns_dir.iterdir())


async def test_failed_upload_is_retried_once_by_the_sweep_then_deleted(tmp_path, monkeypatch):
    platform = Platform()
    platform.upload_status = 500
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    # [DONE] 照样放出：平台下一轮发现转录没到，会开新会话。
    assert frames.body().endswith(b"data: [DONE]\n\n")
    left = list(turns.turns_dir.iterdir())
    assert len(left) == 1 and not (left[0] / "secrets").exists()
    platform.upload_status = 500
    await turns.sweep()
    assert [s for s, _ in platform.uploads] == ["ok", "ok"]
    assert not any(turns.turns_dir.iterdir())


async def test_oversized_transcript_is_reported_not_uploaded(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 8)
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    await turns.run("call-1", "claude", request(tmp_path), Frames())
    assert platform.uploads == [("too_large", b"")]


async def test_cancelled_turn_uploads_in_the_background(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch, hang=True)
    task = asyncio.create_task(turns.run("call-1", "claude", request(tmp_path), Frames()))
    for _ in range(100):
        if turns.active and list(turns.turns_dir.glob("*/config/projects/-work/s1.jsonl")):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait(turns.background, timeout=5)
    assert [s for s, _ in platform.uploads] == ["ok"]
    assert turns.active == set()
    assert not any(turns.turns_dir.iterdir())


async def test_sweep_skips_running_turns_and_drops_unreadable_leftovers(tmp_path):
    platform = Platform()
    turns = make(tmp_path, platform)
    running = turns.turns_dir / "running"
    running.mkdir(parents=True)
    turns.active.add("running")
    junk = turns.turns_dir / "junk"
    junk.mkdir()
    (junk / "meta.json").write_text("not json")
    await turns.sweep()
    assert running.exists() and not junk.exists()
    assert platform.uploads == []


def test_plan_turn_rewrites_paths_and_rejects_what_the_sandbox_cannot_run(tmp_path):
    work = tmp_path / "projects" / "assistant-1"
    (work / "rules").mkdir(parents=True)
    prompt = work / "rules" / "p.md"
    prompt.write_text("x")
    data = request(
        tmp_path,
        system_prompt_file=str(prompt),
        settings=json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}, "model": "m"}),
    )
    plan = plan_turn("claude", data)
    assert plan.body["system_prompt_file"] == "/work/rules/p.md"
    settings = json.loads(plan.body["settings"])
    assert settings["permissions"]["deny"] == ["Bash(rm:*)", "WebSearch", "WebFetch"]
    assert settings["model"] == "m"
    assert (plan.transfer, plan.restore, plan.session_id) == (TRANSFER, False, "s1")
    assert "settings" not in plan_turn("codex", request(tmp_path)).body
    for bad in (
        {"sandbox": "true"},
        {"transcript": {"transfer": "x", "restore": True}},
        {"transcript": None},
        {"worktree": True},
        {"add_dirs": ["/etc"]},
        {"system_prompt_file": "/etc/passwd"},
    ):
        with pytest.raises(ValueError):
            plan_turn("claude", {**request(tmp_path), **bad})


def test_done_gate_holds_the_marker_even_when_split():
    gate = DoneGate()
    out = gate.feed(b"data: a\n\ndata: [DO") + gate.feed(b"NE]\n\n")
    assert out == b"data: a\n\n"
    assert gate.feed(b"trailing") == b""
    assert gate.release() == b"data: [DONE]\n\ntrailing"
    plain = DoneGate()
    assert plain.feed(b"0123456789abcdefghij") + plain.release() == b"0123456789abcdefghij"


def test_messages_cover_every_refusal_code():
    codes = {"sandbox_unavailable", "sandbox_busy", "sandbox_start_failed"}
    codes |= {"credential_missing", "credential_unreadable", "credential_expiring"}
    codes |= {"transcript_unavailable", "unsafe_mount"}
    assert codes <= set(assistant_turns.MESSAGES)


async def test_unsafe_read_only_mount_refuses_the_turn(tmp_path, monkeypatch):
    """额外只读路径包住了本轮目录的上级：不起沙箱，按拒绝处理，不是崩溃。"""
    platform = Platform()
    turns = make(tmp_path, platform)
    turns.daemon.config["assistant_sandbox_ro_paths"] = [str(turns.daemon.data_dir)]
    fake_sandbox(turns, monkeypatch)
    # 保留替身的凭证与 link_cli，只把 _launch 换回真的。
    monkeypatch.setattr(turns, "_launch", AssistantTurns._launch.__get__(turns))
    monkeypatch.setattr(assistant_turns.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(sandbox, "cli_mounts", lambda *_a, **_k: [])

    def no_popen(*_a, **_k):
        raise AssertionError("must not start a sandbox")

    monkeypatch.setattr(assistant_turns.subprocess, "Popen", no_popen)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    assert frames.events[0][2]["status_code"] == 503
    assert json.loads(frames.events[0][1])["error"]["code"] == "unsafe_mount"
    assert platform.uploads == [("unchanged", b"")]
    assert not any(turns.turns_dir.iterdir())
    assert turns.active == set()


async def test_credential_write_failure_refuses_without_logging_paths(
    tmp_path, monkeypatch, caplog
):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    secret_path = str(tmp_path / "secret-place")

    def fail(*_a):
        raise FileExistsError(17, "File exists", secret_path)

    monkeypatch.setattr(turn_credentials, "write", fail)
    frames = Frames()
    with caplog.at_level("WARNING", logger="coreman-runtime"):
        await turns.run("call-1", "claude", request(tmp_path), frames)
    assert json.loads(frames.events[0][1])["error"]["code"] == "sandbox_start_failed"
    assert platform.uploads == [("unchanged", b"")]
    assert "FileExistsError" in caplog.text and secret_path not in caplog.text
    assert not any(turns.turns_dir.iterdir())


async def test_wait_ready_does_not_follow_a_planted_symlink(tmp_path, monkeypatch):
    """socket 目录在沙箱里可写：d.sock 换成指向别处的软链时，守护进程不去连它。"""
    monkeypatch.setattr(assistant_turns, "SOCKET_WAIT_SECONDS", 0.3)
    turns = make(tmp_path, Platform())
    layout = sandbox.TurnLayout.new(turns.turns_dir, turns.daemon.socket_dir)
    layout.create()
    other = Path(tempfile.mkdtemp(dir="/tmp")) / "other.sock"
    connected: list[bool] = []

    async def handle(reader, writer):
        connected.append(True)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(other))
    layout.driver_socket.symlink_to(other)
    process = SimpleNamespace(poll=lambda: None)
    try:
        with pytest.raises(assistant_turns.Refused) as refused:
            await turns._wait_ready(layout, process)  # type: ignore[arg-type]
        assert refused.value.code == "sandbox_start_failed"
        assert connected == []
    finally:
        server.close()
        layout.remove()


def test_hidden_paths_list_only_login_dirs_that_exist(tmp_path):
    turns = make(tmp_path, Platform())
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    turns.daemon.config["home"] = str(home)
    hidden = turns.hidden_paths()
    assert home / ".claude" in hidden and home / ".codex" not in hidden
    assert Path(turns.daemon.config["workspace_root"]) in hidden
    assert turns.daemon.data_dir / "config.json" in hidden


# ---- 第一轮审查的修复 ---------------------------------------------------------


async def test_cleanup_deletes_sandbox_trees_off_the_event_loop(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)
    loop_thread = threading.current_thread()
    calls: list[tuple[Path, bool]] = []
    original = assistant_turns.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        calls.append((Path(path), threading.current_thread() is loop_thread))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(assistant_turns.shutil, "rmtree", rmtree)
    await turns.run("call-1", "claude", request(tmp_path), Frames())
    big = [(p, on_loop) for p, on_loop in calls if p.name != "secrets"]
    assert any(p.parent == turns.turns_dir for p, _ in big)
    assert any(p.parent == turns.daemon.socket_dir for p, _ in big)
    assert not any(on_loop for _, on_loop in big)


async def test_cancel_during_cleanup_still_finishes_every_step(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    entered, release = asyncio.Event(), asyncio.Event()
    # 第 1 次 _stop 在流结束后，第 2 次在清理里。
    fake_sandbox(turns, monkeypatch, stop_gate=(2, entered, release))
    closed: list[bool] = []
    original_close = assistant_turns.egress.Egress.close

    async def close(self):
        closed.append(True)
        await original_close(self)

    monkeypatch.setattr(assistant_turns.egress.Egress, "close", close)
    task = asyncio.create_task(turns.run("call-1", "claude", request(tmp_path), Frames()))
    await asyncio.wait_for(entered.wait(), 5)
    roots = list(turns.turns_dir.iterdir())
    assert len(roots) == 1 and not (roots[0] / "secrets").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait(turns.background, timeout=5)
    assert closed == [True]
    assert not any(turns.turns_dir.iterdir())
    assert not list(turns.daemon.socket_dir.glob("a*"))
    assert turns.active == set()


async def test_refused_turn_cancelled_during_cleanup_leaves_nothing_for_the_sweep(
    tmp_path, monkeypatch
):
    platform = Platform()
    turns = make(tmp_path, platform)
    entered, release = asyncio.Event(), asyncio.Event()
    fake_sandbox(turns, monkeypatch, stop_gate=(1, entered, release))
    monkeypatch.setattr(
        turn_credentials,
        "for_provider",
        lambda *_a: (_ for _ in ()).throw(turn_credentials.Unavailable("credential_expiring")),
    )
    task = asyncio.create_task(turns.run("call-1", "claude", request(tmp_path), Frames()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    if turns.background:
        await asyncio.wait(turns.background, timeout=5)
    await turns.sweep()
    assert platform.uploads == [("unchanged", b"")]
    assert not any(turns.turns_dir.iterdir())
    assert turns.active == set()


def leftover(turns: AssistantTurns, name: str) -> Path:
    root = turns.turns_dir / name
    (root / "config/projects/-work").mkdir(parents=True)
    (root / "config/projects/-work/s1.jsonl").write_bytes(b"{}\n")
    meta = {"provider": "claude", "transfer": TRANSFER, "session_id": "s1"}
    (root / "meta.json").write_text(json.dumps(meta))
    return root


async def test_sweep_survives_a_leftover_whose_pack_fails(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    bad, good = leftover(turns, "a-bad"), leftover(turns, "b-good")
    original = transcripts.pack

    def pack(root, provider, session_id):
        if Path(root).name == "a-bad":
            raise RuntimeError("boom")
        return original(root, provider, session_id)

    monkeypatch.setattr(transcripts, "pack", pack)
    await turns.sweep()
    assert not bad.exists() and not good.exists()
    assert [s for s, _ in platform.uploads] == ["ok"]


async def test_run_whose_pack_fails_removes_the_turn_and_uploads_nothing(tmp_path, monkeypatch):
    platform = Platform()
    turns = make(tmp_path, platform)
    fake_sandbox(turns, monkeypatch)

    def pack(*_a):
        raise RuntimeError("boom")

    monkeypatch.setattr(transcripts, "pack", pack)
    frames = Frames()
    await turns.run("call-1", "claude", request(tmp_path), frames)
    assert frames.body().endswith(b"data: [DONE]\n\n")
    assert platform.uploads == []
    assert not any(turns.turns_dir.iterdir())


async def test_sweep_spares_a_turn_that_registers_after_the_listing(tmp_path, monkeypatch):
    turns = make(tmp_path, Platform())
    root = turns.turns_dir / "late"
    root.mkdir(parents=True)
    run_dir = turns.daemon.socket_dir / "alate"
    run_dir.mkdir()
    original = turns._leftovers

    def listing(active):
        found = original(active)
        turns.active.add("late")
        return found

    monkeypatch.setattr(turns, "_leftovers", listing)
    await turns.sweep()
    assert root.exists() and run_dir.exists()


def test_done_gate_ignores_the_marker_inside_a_content_string():
    gate = DoneGate()
    chunk = b'data: {"content":"data: [DONE]"}\n\n'
    assert gate.feed(chunk) + gate.feed(b"data: [DONE]\n\n") == chunk
    assert gate.release() == b"data: [DONE]\n\n"
    first = DoneGate()
    assert first.feed(b"data: [DONE]\n\n") == b""
    assert first.release() == b"data: [DONE]\n\n"


async def test_transcript_transfers_have_an_overall_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_turns, "TRANSFER_DEADLINE", 0.2)
    turns = make(tmp_path, Platform())

    async def hang(request):
        await asyncio.sleep(3600)

    turns.daemon.http = httpx.AsyncClient(
        transport=httpx.MockTransport(hang), base_url="https://coreman.example.com"
    )
    assert await asyncio.wait_for(turns.upload(TRANSFER, b"x", "ok"), 5) is False
    with pytest.raises(assistant_turns.Refused) as refused:
        await asyncio.wait_for(turns.download(TRANSFER), 5)
    assert refused.value.code == "transcript_unavailable"


def test_plan_turn_requires_a_working_dir_strictly_inside_the_root(tmp_path):
    root = tmp_path / "projects"
    (root / "assistant-1").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    base = request(tmp_path)
    assert plan_turn("claude", base, workspace_root=root).workdir == (root / "assistant-1")
    missing = {k: v for k, v in base.items() if k != "working_dir"}
    for data in (
        missing,
        {**base, "working_dir": str(root)},
        {**base, "working_dir": str(outside)},
        {**base, "working_dir": str(root / "escape")},
        {**base, "working_dir": str(root / "nope")},
    ):
        with pytest.raises(ValueError):
            plan_turn("claude", data, workspace_root=root)
