import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import ChatLog, CronRun, OutboxItem, Task, User, UserReached
from coreman.core.db.session import make_session_factory
from coreman.runtime.bus import instances, tasks
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
    records = (await db_session.scalars(select(ChatLog))).all()
    assert len(records) == 1 and records[0].chat_type == "cron"
    assert await db_session.scalar(select(UserReached)) is None
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None and item.payload["markdown"] == run.reply and item.status == "pending"
    assert run.delivery["outbox_ids"] == [item.id]
    await db_session.refresh(row)
    assert row.running_task_id is None
    await CronRunHandler().run(ctx)
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 1


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
