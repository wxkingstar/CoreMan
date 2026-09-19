"""对话记录一轮一行：开流写进行中，收尾原地改终态；收尸、崩溃、巡检各自结掉进行中的行。"""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.db.models import ChatLog, ChatSession, CronRun, Task, TaskStream, UserReached
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler import reaper
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx, seed_bot


def _entry(bot, task_id: int | None, **over: object) -> ChatLogEntry:  # type: ignore[no-untyped-def]
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="wecom",
        chat_type="single",
        message_type="text",
        status="running",
        request_at=datetime.now(UTC) - timedelta(seconds=3),
        user_id=bot.created_by,
        chat_id="zs",
        session_key="zs",
        relay_session_id=uuid.uuid4(),
        model="m",
        task_id=task_id,
        message_content="问题",
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def _task(session: AsyncSession, bot, kind: str = "chat") -> Task:  # type: ignore[no-untyped-def]
    task = await tasks.enqueue(
        session,
        NewTask(bot_id=bot.id, kind=kind, payload={}, session_key=f"k-{uuid.uuid4()}"),
    )
    await session.commit()
    assert task is not None
    return task


async def _logs(session: AsyncSession, task_id: int) -> list[ChatLog]:
    rows = await session.scalars(
        select(ChatLog)
        .where(ChatLog.task_id == task_id)
        .order_by(ChatLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def test_open_then_finish_is_one_row_updated_in_place(db_session: AsyncSession) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    opened = _entry(bot, task.id, response_content="不该写进去", latency_ms=5)
    assert await chat_logs.open_turn(db_session, opened)
    await db_session.commit()
    (row,) = await _logs(db_session, task.id)
    # 进行中：没有耗时、回复与响应时间；私聊可达在开流时就记下。
    assert row.status == "running" and row.latency_ms is None and row.response_at is None
    assert row.message_content == "问题" and row.relay_session_id == opened.relay_session_id
    assert row.response_content == "不该写进去"
    assert await db_session.scalar(select(UserReached)) is not None
    done = dataclasses.replace(
        opened,
        status="success",
        message_content="组装后的问题",
        response_content="回答",
        latency_ms=3000,
        tools_used=["Bash", "Bash"],
        response_at=datetime.now(UTC),
    )
    assert await chat_logs.finish_turn(db_session, done)
    await db_session.commit()
    (after,) = await _logs(db_session, task.id)
    assert after.id == row.id and after.status == "success" and after.latency_ms == 3000
    assert after.message_content == "组装后的问题" and after.response_content == "回答"
    assert after.tools_used == ["Bash"]
    # 已经结束的行不会被再次收尾改写，也不会多出一行。
    assert await chat_logs.finish_turn(db_session, dataclasses.replace(done, status="error"))
    assert not await chat_logs.close_running(
        db_session, task.id, status="timeout", error_code="x", error_message=None
    )
    await db_session.commit()
    (again,) = await _logs(db_session, task.id)
    assert again.status == "success"


async def test_open_turn_keeps_working_on_a_reused_connection(db_session: AsyncSession) -> None:
    """同一连接上反复开流：预备语句换成通用计划之后，部分唯一索引照样认得出来。"""
    bot, _, _ = await seed_bot(db_session)
    for _ in range(8):
        task = await _task(db_session, bot)
        assert await chat_logs.open_turn(db_session, _entry(bot, task.id))
        # 同一任务再开一次（不该发生）也不报错、不多写一行。
        assert await chat_logs.open_turn(db_session, _entry(bot, task.id))
        await db_session.commit()
        assert len(await _logs(db_session, task.id)) == 1


async def test_finish_without_running_row_inserts_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    # 开流前就结束（或开流时没写成）：收尾补一行。
    assert await chat_logs.finish_turn(db_session, _entry(bot, task.id, status="error"))
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine))
    assert await writer.write(_entry(bot, task.id, status="timeout"))
    assert await writer.finish(_entry(bot, task.id, status="timeout"))
    assert [r.status for r in await _logs(db_session, task.id)] == ["error"]


async def test_close_running_keeps_what_the_turn_recorded(db_session: AsyncSession) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    opened = _entry(bot, task.id)
    await chat_logs.open_turn(db_session, opened)
    await db_session.commit()
    assert await chat_logs.close_running(
        db_session, task.id, status="timeout", error_code="worker_lost", error_message="x" * 9000
    )
    await db_session.commit()
    (row,) = await _logs(db_session, task.id)
    assert row.status == "timeout" and row.error_code == "worker_lost"
    assert len(row.error_message or "") == chat_logs.LIMITS["error_message"]
    assert row.message_content == "问题" and row.relay_session_id == opened.relay_session_id
    assert row.latency_ms is not None and row.latency_ms >= 3000


async def test_log_failures_roll_back_only_their_savepoint(db_session: AsyncSession) -> None:
    """写日志失败不能拖垮调用方的事务：任务终态照样提交。"""
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    await tasks.finish(db_session, task.id, status="failed", error_code="boom")
    # 违反 chat_logs 的检查约束。
    assert not await chat_logs.open_turn(db_session, _entry(bot, task.id, chat_type="bogus"))
    assert not await chat_logs.finish_turn(db_session, _entry(bot, task.id, status="bogus"))
    await db_session.commit()
    row = await tasks.get(db_session, task.id)
    assert row is not None and row.status == "failed"
    assert await _logs(db_session, task.id) == []


async def test_amend_only_touches_the_finished_row(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    await chat_logs.open_turn(db_session, _entry(bot, task.id, response_content="半截"))
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine))
    await writer.amend_response(task.id, "终稿 + 额度表")
    assert (await _logs(db_session, task.id))[0].response_content == "半截"
    await chat_logs.finish_turn(db_session, _entry(bot, task.id, status="success"))
    await db_session.commit()
    await writer.amend_response(task.id, "终稿 + 额度表")
    assert (await _logs(db_session, task.id))[0].response_content == "终稿 + 额度表"


async def test_chat_turn_is_visible_as_running_then_finished_in_place(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "第一轮就要能看")
    fake = FakeRelay("slow", chunk_delay=0.2)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: fake.client())
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        for _ in range(200):
            if fake.requests:
                break
            await asyncio.sleep(0.05)
        assert fake.requests
        # 模型还在回答：记录已经在库里，会话与链接里的是同一个。
        (running,) = await _logs(db_session, task.id)
        sid = (await db_session.scalar(select(ChatSession))).relay_session_id  # type: ignore[union-attr]
        stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
        assert running.status == "running" and running.relay_session_id == sid
        assert stream is not None and str(sid) in (stream.session_url or "")
        assert "?t=" not in (stream.session_url or "")
        assert running.stream_id == stream.stream_id and running.message_content
        await asyncio.wait_for(runner, 15)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    (done,) = await _logs(db_session, task.id)
    assert done.id == running.id and done.status == "success"
    assert done.response_content and done.latency_ms is not None and done.input_tokens == 100


async def test_opening_turn_is_skipped_by_the_reaper_but_not_by_heartbeats(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """开流事务提交前，收尸跳过这个任务（否则收尸补一行、开流再写一行）；心跳照常写得进去。"""
    from sqlalchemy import update

    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "开流中")
    ctx = build_ctx(db_engine, task)
    handler = ChatTaskHandler()
    factory = make_session_factory(db_engine)
    async with factory() as session:
        resolved = await handler._resolve(session, ctx)
        await session.commit()
    assert resolved is not None
    intake, relay, _ = resolved
    stale = datetime.now(UTC) - timedelta(seconds=120)
    async with factory() as opening:
        await handler._open(opening, ctx, intake, relay, ctx.clock())
        async with factory() as other:
            await asyncio.wait_for(
                other.execute(update(Task).where(Task.id == task.id).values(heartbeat_at=stale)),
                5,
            )
            await other.commit()
            assert await reaper.reap_lost_tasks(other, datetime.now(UTC)) == 0
            await other.commit()
        await opening.commit()
    (row,) = await _logs(db_session, task.id)
    assert row.status == "running"
    # 开流提交之后才轮到收尸：结掉的是同一行。
    assert await reaper.reap_lost_tasks(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    assert [(r.status, r.error_code) for r in await _logs(db_session, task.id)] == [
        ("timeout", "worker_lost")
    ]


async def test_reaper_closes_the_running_row_instead_of_adding_one(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _task(db_session, bot)
    await instances.register(
        db_session, instance_id="w1", service="worker", version="dev", capacity=1
    )
    await db_session.commit()
    assert await tasks.claim(db_session, lane="normal", instance_id="w1")
    await tasks.start(db_session, task.id)
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="s",
        reply_context={"req_id": "r"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await chat_logs.open_turn(db_session, _entry(bot, task.id))
    row = await tasks.get(db_session, task.id)
    assert row is not None
    now = datetime.now(UTC)
    row.heartbeat_at = now - timedelta(seconds=61)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    assert await reaper.reap_lost_tasks(db_session, now, chat_logs_factory=factory) == 1
    await db_session.commit()
    (log,) = await _logs(db_session, task.id)
    assert log.status == "timeout" and log.error_code == "worker_lost"
    assert log.message_content == "问题" and log.relay_session_id is not None


async def test_abandoned_turns_are_closed_after_the_grace_period(db_session: AsyncSession) -> None:
    bot, _, _ = await seed_bot(db_session)
    now = datetime.now(UTC)
    grace = timedelta(seconds=reaper.ABANDONED_TURN_GRACE_SECONDS + 1)
    old = now - grace
    cases: dict[str, Task] = {}
    for name, status in (
        ("succeeded_old", "succeeded"),
        ("cancelled_old", "cancelled"),
        ("failed_recent", "failed"),
        ("active", "running"),
    ):
        cases[name] = await _task(db_session, bot)
        if status != "running":
            await tasks.finish(db_session, cases[name].id, status=status, error_code=f"c-{name}")
        await chat_logs.open_turn(db_session, _entry(bot, cases[name].id, request_at=old))
    await db_session.commit()
    for name in ("succeeded_old", "cancelled_old"):
        row = await db_session.get(Task, cases[name].id)
        assert row is not None
        row.finished_at = old
    # 任务行已经没了（删机器人级联删除）：旧的结掉，刚开的不动。
    await chat_logs.open_turn(db_session, _entry(bot, 10_000_001, request_at=old))
    await chat_logs.open_turn(db_session, _entry(bot, 10_000_002))
    await db_session.commit()
    assert await reaper.close_abandoned_turns(db_session, now) == 3
    await db_session.commit()

    async def state(task_id: int) -> tuple[str, str | None]:
        (row,) = await _logs(db_session, task_id)
        return row.status, row.error_code

    assert await state(cases["succeeded_old"].id) == ("success", "c-succeeded_old")
    assert await state(cases["cancelled_old"].id) == ("stopped", "c-cancelled_old")
    assert await state(cases["failed_recent"].id) == ("running", None)
    assert await state(cases["active"].id) == ("running", None)
    assert await state(10_000_001) == ("error", "task_missing")
    assert await state(10_000_002) == ("running", None)
    assert await reaper.close_abandoned_turns(db_session, now) == 0


async def test_cron_run_logs_running_then_finishes_in_place(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.test_cron_scheduler import job

    now = datetime.now(UTC)
    await job(db_session, now, target_chats=["test-group"])
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("slow", chunk_delay=0.2)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    runner = asyncio.create_task(CronRunHandler().run(ctx))
    try:
        for _ in range(200):
            if fake.requests:
                break
            await asyncio.sleep(0.05)
        assert fake.requests
        (running,) = await _logs(db_session, task.id)
        assert running.status == "running" and running.chat_type == "cron"
        assert str(running.relay_session_id) == fake.requests[0]["session_id"]
        await asyncio.wait_for(runner, 15)
    finally:
        runner.cancel()
    (done,) = await _logs(db_session, task.id)
    run = await db_session.scalar(select(CronRun))
    assert done.id == running.id and done.status == "success"
    assert run is not None and done.response_content == run.reply


async def test_cron_recovery_closes_the_running_row(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.crypto import Cipher
    from coreman.runtime.scheduler.cron import recover_runs, run_tick
    from tests.integration.test_cron_handler import claim
    from tests.integration.test_cron_scheduler import job

    now = datetime.now(UTC)
    row = await job(db_session, now, target_chats=["test-group"])
    factory = make_session_factory(db_engine)
    await run_tick(factory, now)
    task = await claim(db_session)
    bot_id = row.bot_id
    from coreman.core.db.models import Bot

    bot = await db_session.get(Bot, bot_id)
    await chat_logs.open_turn(
        db_session, _entry(bot, task.id, chat_type="cron", message_content="日报")
    )
    await tasks.finish(db_session, task.id, status="failed", error_code="worker_lost")
    await db_session.commit()
    assert await recover_runs(factory, now, Cipher(b"x" * 32)) == 1
    (log,) = await _logs(db_session, task.id)
    assert log.status == "error" and log.error_code == "worker_lost"
    assert log.message_content == "日报" and log.relay_session_id is not None
