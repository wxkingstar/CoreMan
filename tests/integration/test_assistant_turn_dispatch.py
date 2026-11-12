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


@pytest.mark.parametrize("breakage", ["offline", "no_sandbox", "not_in_pool", "shared"])
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
    elif breakage == "not_in_pool":
        relay_row.assistant_pool = False
    else:
        from tests.integration.assistant_helpers import seed_standard

        standard = await seed_standard(db_session)
        standard.relay_server_id = relay_row.id
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
    log = RecordingLog()
    done, relay = await run_crashing(db_engine, db_session, task, log)
    assert relay.requests == []
    assert done.status == "failed"
    assert done.error_code == "AssistantDispatchError"
    assert "secret-internal-detail" not in (done.error_message or "")
    logged = str(log.calls)
    assert "secret-internal-detail" not in logged
    assert "RuntimeError" in logged  # only the exception type name is logged
    assert not any(name == "exception" for name, _ in log.calls)


class RecordingLog:
    """记下处理器写过哪些日志（事件名与字段），用来断言日志里没有异常的原文与堆栈。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, (args, kwargs)))

        return record


async def run_crashing(
    engine: AsyncEngine, session: AsyncSession, task: Task, log: RecordingLog
) -> tuple[Task, FakeRelay]:
    """经 WorkerService 的失败收口跑一轮：处理器抛错时任务怎么落。"""
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.worker.service import WorkerService

    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    object.__setattr__(ctx, "log", log)
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


async def test_first_turn_after_a_switch_keeps_the_carried_context(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """换运行时后的首轮：新会话号还没派出过，不换掉它，平台的上下文说明照样带进这一轮。"""
    import uuid

    from coreman.core.chat import sessions
    from tests.integration.worker_helpers import turn_block

    bot, task = await assistant_chat(db_session)
    _, first = await run(db_engine, db_session, task)
    # 上一轮的转录已交回（交接办完）。
    row = await db_session.scalar(
        select(AssistantTranscript).where(AssistantTranscript.bot_id == bot.id)
    )
    assert row is not None
    row.pending_transfer, row.pending_node_id = None, None
    await db_session.commit()
    await sessions.carry_over(db_session, bot.id, backend="claude", keep=set())
    await db_session.commit()
    second_task = await chat_task(
        db_session, bot, "再查一次", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    done, second = await run(db_engine, db_session, second_task)
    assert done.status == "succeeded"
    body = second.requests[0]
    assert body["transcript"]["restore"] is False
    assert body["session_id"] != first.requests[0]["session_id"]
    assert uuid.UUID(body["session_id"])
    assert "上下文续接" in turn_block(body)


@pytest.mark.parametrize("scenario", ["quota", "normal_observed_model", "relay_error"])
async def test_side_channel_chunks_of_a_sandboxed_turn_touch_no_relay_records(
    db_engine: AsyncEngine, db_session: AsyncSession, scenario: str
) -> None:
    """沙箱里的进程能伪造额度、运行时与报错帧：助理这一轮不改实例的额度与健康记录。"""
    bot, task = await assistant_chat(db_session)
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    relay = FakeRelay(scenario)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert relay.requests and relay.requests[0]["sandbox"] is True
    await db_session.refresh(relay_row)
    assert relay_row.rate_limit_probed_at is None and relay_row.quota_observation is None
    assert relay_row.rate_limit_5h_used_pct is None
    assert relay_row.model_health is None and relay_row.health_checked_at is None
    assert relay_row.last_model_failure is None


async def test_an_unsandboxed_pool_refusal_is_not_a_provider_failure(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """普通员工（绕过了绑定检查）落到助理池实例上：RelayClient 当场拒绝，不记成模型故障。"""
    from coreman.core.relay.client import RelayClient
    from tests.integration.worker_helpers import seed_bot

    bot, relay_row, _ = await seed_bot(db_session)
    relay_row.assistant_pool = True
    await db_session.commit()
    task = await chat_task(db_session, bot, "你好")
    fake = FakeRelay("normal")

    def pool_client(_relay):  # type: ignore[no-untyped-def]
        import httpx

        return RelayClient(
            fake.base_url,
            http=httpx.AsyncClient(base_url=fake.base_url, transport=fake.transport),
            assistant_pool=True,
        )

    ctx = build_ctx(db_engine, task, relay_client_factory=pool_client)
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert fake.requests == []
    await db_session.refresh(relay_row)
    assert relay_row.model_health is None and relay_row.last_model_failure is None
    assert relay_row.health_checked_at is None


class QueueTimeoutRelay(FakeRelay):
    """排队等满仍没有节点来领：传输层报 RuntimeQueueTimeout，`unclaimed` 说节点是否确定没见过。"""

    def __init__(self, unclaimed: bool) -> None:
        super().__init__("normal")
        self.unclaimed = unclaimed

    async def _handle(self, request):  # type: ignore[no-untyped-def]
        from coreman.core.runtime_nodes.transport import RuntimeQueueTimeout

        if request.url.path == "/v1/chat/completions":
            self.requests.append({})
            raise RuntimeQueueTimeout("排队超时", request=request, unclaimed=self.unclaimed)
        return await super()._handle(request)


@pytest.mark.parametrize("unclaimed", [True, False])
async def test_a_turn_no_node_ever_took_releases_its_handover(
    db_engine: AsyncEngine, db_session: AsyncSession, unclaimed: bool
) -> None:
    """排队超时、确定没到过节点：交接编号作废，上一份转录下一轮照旧可续；有一点不确定就留着。"""
    bot, task = await assistant_chat(db_session)
    relay = QueueTimeoutRelay(unclaimed)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert relay.requests
    row = await db_session.scalar(
        select(AssistantTranscript)
        .where(AssistantTranscript.bot_id == bot.id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    assert (row.pending_transfer is None) is unclaimed
    assert (row.pending_node_id is None) is unclaimed


async def test_release_only_clears_the_handover_it_was_given(db_session: AsyncSession) -> None:
    import uuid

    from coreman.core.assistants import transcripts

    bot, _task = await assistant_chat(db_session)
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    session_id, current = uuid.uuid4(), uuid.uuid4()
    db_session.add(
        AssistantTranscript(
            bot_id=bot.id,
            session_id=session_id,
            pending_transfer=current,
            pending_node_id=relay_row.runtime_node_id,
        )
    )
    await db_session.commit()
    assert not await transcripts.release(
        db_session, bot_id=bot.id, session_id=session_id, transfer=uuid.uuid4()
    )
    assert await transcripts.release(
        db_session, bot_id=bot.id, session_id=session_id, transfer=current
    )
    await db_session.commit()
    row = await db_session.get(AssistantTranscript, (bot.id, session_id), populate_existing=True)
    assert row is not None and row.pending_transfer is None and row.pending_node_id is None
