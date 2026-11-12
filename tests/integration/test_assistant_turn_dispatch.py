"""worker 派出个人助理的一轮：带上沙箱与转录交接；所在运行时不行就暂停并告诉所有者。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import (
    AssistantTranscript,
    Bot,
    RelayServer,
    RuntimeNode,
    Task,
    TaskStream,
)
from coreman.core.runtime_nodes.transport import now
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import into_assistant
from tests.integration.credential_helpers import owner
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")


async def assistant_chat(session: AsyncSession) -> tuple[Bot, Task]:
    bot, user, task, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    await session.commit()
    return bot, task


async def run(engine: AsyncEngine, session: AsyncSession, task: Task) -> tuple[Task, FakeRelay]:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def test_assistant_turn_is_sent_sandboxed_with_a_handover(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, task = await assistant_chat(db_session)
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "succeeded"
    body = relay.requests[0]
    assert body["sandbox"] is True
    row = await db_session.scalar(
        select(AssistantTranscript).where(AssistantTranscript.bot_id == bot.id)
    )
    assert row is not None and row.pending_transfer is not None
    assert body["transcript"] == {"transfer": row.pending_transfer.hex, "restore": False}
    assert str(row.session_id) == body["session_id"]


async def test_unfinished_handover_starts_a_new_session_that_reaches_the_node(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """上一轮的转录没传回来：prepare 换了新会话，请求里带的就是换后的会话与 restore=False。"""
    bot, task = await assistant_chat(db_session)
    _, first = await run(db_engine, db_session, task)
    second_task = await chat_task(
        db_session, bot, "再查一次", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    done, second = await run(db_engine, db_session, second_task)
    assert done.status == "succeeded"
    body = second.requests[0]
    assert body["sandbox"] is True and body["transcript"]["restore"] is False
    assert body["session_id"] != first.requests[0]["session_id"]
    row = await db_session.scalar(
        select(AssistantTranscript).where(
            AssistantTranscript.bot_id == bot.id,
            AssistantTranscript.pending_transfer.is_not(None),
        )
    )
    assert row is not None and str(row.session_id) == body["session_id"]
    assert body["transcript"]["transfer"] == row.pending_transfer.hex


@pytest.mark.parametrize("breakage", ["offline", "no_sandbox", "not_in_pool"])
async def test_assistant_pauses_instead_of_running_elsewhere(
    db_engine: AsyncEngine, db_session: AsyncSession, breakage: str
) -> None:
    bot, task = await assistant_chat(db_session)
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    node = await db_session.get(RuntimeNode, relay_row.runtime_node_id)
    assert node is not None
    if breakage == "offline":
        node.heartbeat_at = now() - timedelta(minutes=5)
    elif breakage == "no_sandbox":
        node.capabilities = {relay_row.model_provider: {"installed": True, "login": "ready"}}
    else:
        relay_row.assistant_pool = False
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "failed" and done.error_code == "assistant_paused"
    assert relay.requests == []
    stream = await db_session.get(TaskStream, task.id, populate_existing=True)
    assert stream is not None and stream.is_complete
    text = stream.final_text or ""
    assert text.startswith("你的个人助理暂停了：")
    assert str(node.id) not in text and node.name not in text


@pytest.mark.parametrize("failing", ["gate", "prepare"])
async def test_assistant_turn_is_not_sent_when_the_gate_or_prepare_raises(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    from coreman.core.assistants import runtime, transcripts

    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret-internal-detail")

    if failing == "gate":
        monkeypatch.setattr(runtime, "turn_blocker", boom)
    else:
        monkeypatch.setattr(transcripts, "prepare", boom)
    _, task = await assistant_chat(db_session)
    done, relay = await run_crashing(db_engine, db_session, task)
    assert relay.requests == []
    assert done.status == "failed"
    assert done.error_code == "AssistantDispatchError"
    assert "secret-internal-detail" not in (done.error_message or "")


async def run_crashing(
    engine: AsyncEngine, session: AsyncSession, task: Task
) -> tuple[Task, FakeRelay]:
    """经 WorkerService 的失败收口跑一轮：处理器抛错时任务怎么落。"""
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.worker.service import WorkerService

    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    service = WorkerService.__new__(WorkerService)
    service._factory = make_session_factory(engine)
    try:
        await ChatTaskHandler().run(ctx)
    except Exception as exc:  # noqa: BLE001
        await service._fail(ctx, type(exc).__name__, str(exc))
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def test_standard_turn_never_touches_the_assistant_path(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.assistants import runtime, transcripts
    from coreman.runtime.worker.chat import assistant_runtime

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("assistant path used for a standard bot")

    monkeypatch.setattr(assistant_runtime, "paused", forbidden)
    monkeypatch.setattr(runtime, "turn_blocker", forbidden)
    monkeypatch.setattr(transcripts, "prepare", forbidden)
    _, _, task, _ = await owner(db_session)
    done, relay = await run(db_engine, db_session, task)
    assert done.status == "succeeded"
    assert "sandbox" not in relay.requests[0] and "transcript" not in relay.requests[0]
    assert await db_session.scalar(select(AssistantTranscript)) is None
