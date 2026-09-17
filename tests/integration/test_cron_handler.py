import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.db.models import ChatLog, CronRun, OutboxItem, Task, User, UserReached
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler.cron import recover_runs, run_tick
from coreman.runtime.worker.cron_handler import CronRunHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_cron_scheduler import job
from tests.integration.worker_helpers import build_ctx


async def claim(session: AsyncSession) -> Task:
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    task = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert task is not None
    return task


async def test_cron_fresh_identity_atomic_log_and_delivery(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    row = await job(db_session, now, target_chats=["test-group"])
    factory = make_session_factory(db_engine)
    await run_tick(factory, now)
    task = await claim(db_session)
    # 排队后编辑不影响已触发任务。
    await db_session.refresh(row)
    row.prompt = "changed-after-enqueue"
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "success" and run.reply == "你好，世界。"
    assert run.prompt == "请生成日报" and run.input_tokens == 100
    assert len(fake.requests) == 1
    env = fake.requests[0]["env_vars"]
    assert env["COREMAN_USER_LOGIN"] == "creator" and env["COREMAN_CHAT_TYPE"] == "cron"
    system_prompt = fake.requests[0]["messages"][0]["content"]
    assert "# Scheduled Run Constraints" in system_prompt
    records = (await db_session.scalars(select(ChatLog))).all()
    assert len(records) == 1 and records[0].chat_type == "cron"
    assert await db_session.scalar(select(UserReached)) is None
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None and item.status == "pending"
    pushed = item.payload["markdown"]
    from coreman.core.i18n.messages import msg

    # 推送带任务名/机器人/耗时的头与「定时推送」尾，存档的 reply 仍是原文。
    assert pushed.startswith(f"**{run.job_name}**\n> 机器人：销售 | 耗时：")
    assert run.reply in pushed and pushed.endswith(msg("cron_push_footer"))
    assert run.delivery["outbox_ids"] == [item.id] and "fallback_user_id" not in run.delivery
    await db_session.refresh(row)
    assert row.running_task_id is None
    await CronRunHandler().run(ctx)
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 1


async def test_oversized_result_is_truncated_and_still_delivered(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    from coreman.core.i18n.messages import msg
    from coreman.runtime.worker import cron_handler

    monkeypatch.setattr(cron_handler, "RESULT_MAX_CHARS", 3)
    now = datetime.now(UTC)
    await job(db_session, now, target_chats=["test-group"])
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    notice = msg("cron_result_truncated", limit=3)
    assert run is not None and run.status == "success" and run.error_message is None
    assert run.reply == "你好，" + notice
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None and run.reply in item.payload["markdown"]
    assert (await tasks.get(db_session, task.id)).status == "succeeded"  # type: ignore[union-attr]


async def test_precheck_failures_and_skip_do_not_call_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    row = await job(
        db_session,
        now,
        precheck_script='def should_trigger(ctx):\n return {"trigger": False, "reason": "quiet"}',
    )
    factory = make_session_factory(db_engine)
    await run_tick(factory, now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "skipped"
    assert not fake.requests and await db_session.scalar(select(OutboxItem)) is None
    await db_session.refresh(row)
    row.precheck_script = 'import os\ndef should_trigger(ctx):\n return {"trigger": True}'
    row.next_run_at = now + timedelta(minutes=1)
    await db_session.commit()
    await run_tick(factory, now + timedelta(minutes=1))
    task = await claim(db_session)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun).order_by(CronRun.id.desc()))
    assert run is not None and run.status == "failed_precheck"
    assert not fake.requests


async def test_disabled_actor_and_lost_worker_recovery(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    row = await job(db_session, now, target_chats=["test-group"])
    factory = make_session_factory(db_engine)
    await run_tick(factory, now)
    task = await claim(db_session)
    actor = await db_session.get(User, row.created_by)
    actor.status = "disabled"
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    assert not fake.requests
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "failed"
    actor.status = "active"
    await db_session.refresh(row)
    row.next_run_at = now + timedelta(minutes=1)
    await db_session.commit()
    await run_tick(factory, now + timedelta(minutes=1))
    task = await claim(db_session)
    await tasks.finish(db_session, task.id, status="failed", error_code="worker_lost")
    await db_session.commit()
    # run_tick 不抢先清除尚未恢复的关联。
    await run_tick(factory, now + timedelta(minutes=2))
    counts = await asyncio.gather(
        recover_runs(factory, now, ctx.cipher), recover_runs(factory, now, ctx.cipher)
    )
    assert sum(counts) == 1
    runs = (await db_session.scalars(select(CronRun))).all()
    await db_session.refresh(runs[-1])
    assert runs[-1].status == "failed" and runs[-1].delivery["outbox_ids"]
    from coreman.core.i18n.messages import msg

    lost = await db_session.get(OutboxItem, runs[-1].delivery["outbox_ids"][0])
    assert lost is not None and lost.payload["markdown"] == msg(
        "cron_failed", name=runs[-1].job_name, bot="销售", reason=msg("cron_worker_lost")
    )
    assert len((await db_session.scalars(select(ChatLog))).all()) == 2


async def test_cancelled_cron_never_starts_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    await job(db_session, now)
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    await tasks.request_cancel(db_session, task.id, "cron_cancelled")
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    assert not fake.requests
    await db_session.refresh(task)
    assert task.status == "cancelled"


async def test_private_delivery_revalidates_identity(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.chat.reachability import private_target_valid

    now = datetime.now(UTC)
    row = await job(db_session, now)
    row.target_users = [row.created_by]
    db_session.add(
        UserReached(bot_id=row.bot_id, user_id=row.created_by, platform_chat_id="private-chat")
    )
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    await CronRunHandler().run(
        build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    )
    item = await db_session.scalar(select(OutboxItem))
    assert item.target["recipient_user_id"] == str(row.created_by)
    assert await private_target_valid(db_session, item)
    actor = await db_session.get(User, row.created_by)
    actor.status = "disabled"
    await db_session.commit()
    assert not await private_target_valid(db_session, item)


async def test_result_without_any_target_falls_back_to_creator(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    row = await job(db_session, now)
    db_session.add(
        UserReached(bot_id=row.bot_id, user_id=row.created_by, platform_chat_id="creator-chat")
    )
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    await CronRunHandler().run(
        build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    )
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "success"
    # 一个接收人、群、邮箱、webhook 都没配：跑成功的结果不能静默无人收，兜底私聊给创建者。
    assert run.delivery["fallback_user_id"] == str(row.created_by)
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None and item.target["chat_id"] == "creator-chat"
    assert item.target["recipient_user_id"] == str(row.created_by)
    assert run.reply is not None and run.reply in item.payload["markdown"]


async def test_long_precheck_does_not_stall_heartbeats(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import time as _time

    from coreman.core.cron import precheck

    def slow(script, ctx, timeout_seconds=30, *, stop=None):  # type: ignore[no-untyped-def]
        _time.sleep(0.8)  # 纯 CPU/阻塞：放在事件循环上会让下面的心跳一次都写不进去
        return precheck.PrecheckResult(False, reason="slow")

    monkeypatch.setattr(precheck, "run_precheck", slow)
    now = datetime.now(UTC)
    await job(db_session, now, precheck_script="def should_trigger(ctx):\n pass")
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    beats = 0

    async def beating() -> None:
        nonlocal beats
        while True:
            await ctx.heartbeat()
            beats += 1
            await asyncio.sleep(0.1)

    beat = asyncio.create_task(beating())
    await asyncio.sleep(0)
    before = beats
    try:
        await CronRunHandler().run(ctx)
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "skipped" and not fake.requests
    assert beats - before >= 4


async def test_cron_relay_error_without_finish_keeps_reason(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    await job(db_session, now, target_chats=["test-group"])
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("relay_error_no_finish")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "failed"
    assert run.error_message is not None and run.error_message.startswith("x_relay_error: ")
    assert "codex produced no output" in run.error_message
    row = await db_session.get(Task, task.id, populate_existing=True)
    assert row is not None and row.error_code == "x_relay_error"
    log = await db_session.scalar(select(ChatLog))
    assert log is not None and log.error_code == "x_relay_error"
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None and "codex produced no output" in item.payload["markdown"]
    # 失败推送与成功推送同一套标记：任务名、机器人、原因；不带「定时推送」尾巴。
    assert item.payload["markdown"].startswith("**定时任务执行失败**\n> 任务：")
    assert "> 机器人：销售\n> 原因：x_relay_error" not in item.payload["markdown"]
    assert "> 原因：[codex error]" in item.payload["markdown"]


async def test_cron_handler_leaves_periodic_heartbeats_to_the_service(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    await job(db_session, now)
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("slow", chunk_delay=0.05)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    calls = 0
    original = ctx.heartbeat

    async def counting() -> None:
        nonlocal calls
        calls += 1
        await original()

    ctx.heartbeat = counting  # type: ignore[method-assign]
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "success"
    # 只剩外部调用前那一次显式收取取消；周期心跳由 WorkerService 统一写。
    assert calls == 1


@pytest.mark.parametrize("skip", [False, True])
async def test_once_worker_finish_and_repeated_handler_do_not_run_again(
    db_engine, db_session, skip
):
    now = datetime.now(UTC)
    row = await job(
        db_session,
        now,
        schedule_kind="once",
        run_at=now,
        target_chats=["test-group"],
        precheck_script='def should_trigger(ctx):\n return {"trigger": False}' if skip else None,
    )
    factory = make_session_factory(db_engine)
    await run_tick(factory, now)
    task = await claim(db_session)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    await db_session.refresh(row)
    assert row.last_status == ("skipped" if skip else "success")
    assert not row.enabled and row.consumed_at is not None
    await CronRunHandler().run(ctx)
    assert await run_tick(factory, now + timedelta(days=366)) == 0
    assert len(fake.requests) == (0 if skip else 1)
    assert len((await db_session.scalars(select(OutboxItem))).all()) == (0 if skip else 1)
