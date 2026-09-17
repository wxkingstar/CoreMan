import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import CronJob, CronRun, Task, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler.cron import run_tick
from tests.integration.worker_helpers import seed_bot


async def job(session: AsyncSession, now: datetime, **kw: object) -> CronJob:
    bot, _, _ = await seed_bot(session)
    session.add(UserIdentity(user_id=bot.created_by, platform="wecom", platform_user_id="creator"))
    row = CronJob(
        bot_id=bot.id,
        created_by=bot.created_by,
        name="日报",
        cron_expression="* * * * *",
        prompt="请生成日报",
        next_run_at=now,
        **kw,
    )
    session.add(row)
    await session.commit()
    return row


async def test_atomic_claim_and_no_overlap(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    row = await job(db_session, now)
    factory = make_session_factory(db_engine)
    counts = await asyncio.gather(run_tick(factory, now), run_tick(factory, now))
    assert sum(counts) == 1
    tasks = (await db_session.scalars(select(Task))).all()
    assert len(tasks) == 1 and tasks[0].user_id == row.created_by
    assert tasks[0].payload["config"]["prompt"] == "请生成日报"
    runs = (await db_session.scalars(select(CronRun))).all()
    assert len(runs) == 1 and runs[0].executed_by == row.created_by
    assert await run_tick(factory, now + timedelta(minutes=1)) == 0
    assert len((await db_session.scalars(select(Task))).all()) == 1
    assert (await db_session.scalar(select(CronRun).order_by(CronRun.id.desc()))).precheck_meta[
        "reason"
    ] == "previous_run_active"


async def test_misfire_and_expiry(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    row = await job(db_session, now - timedelta(seconds=301))
    factory = make_session_factory(db_engine)
    assert await run_tick(factory, now) == 1
    assert await db_session.scalar(select(Task)) is None
    assert (await db_session.scalar(select(CronRun))).status == "skipped"
    await db_session.refresh(row)
    row.expires_at = now
    await db_session.commit()
    await run_tick(factory, now)
    await db_session.refresh(row)
    assert not row.enabled


async def test_manual_identity_and_disabled_creator(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import BotMember, User

    now = datetime.now(UTC)
    row = await job(db_session, now + timedelta(hours=1))
    actor = User(login_name="clicker", display_name="点击者")
    db_session.add(actor)
    await db_session.flush()
    db_session.add_all(
        [
            BotMember(bot_id=row.bot_id, user_id=actor.id),
            UserIdentity(user_id=actor.id, platform="wecom", platform_user_id="clicker"),
        ]
    )
    row.force_run_at = now
    row.force_run_by = actor.id
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), now)
    task = await db_session.scalar(select(Task))
    assert task is not None and task.user_id == actor.id
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.trigger_kind == "manual" and run.executed_by == actor.id


async def test_once_claim_consumes_atomically_and_recovery_never_rearms(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.crypto import Cipher
    from coreman.runtime.scheduler.cron import recover_runs

    cipher = Cipher(b"x" * 32)

    now = datetime.now(UTC)
    row = await job(db_session, now, schedule_kind="once", run_at=now)
    factory = make_session_factory(db_engine)
    assert sum(await asyncio.gather(run_tick(factory, now), run_tick(factory, now))) == 1
    await db_session.refresh(row)
    assert row.consumed_at == now and row.next_run_at is None and row.enabled
    assert await run_tick(factory, now + timedelta(minutes=1)) == 0
    task = await db_session.get(Task, row.running_task_id)
    task.status = "failed"
    await db_session.commit()
    assert await recover_runs(factory, now, cipher) == 1
    await db_session.refresh(row)
    assert not row.enabled and row.running_task_id is None
    assert await run_tick(factory, now + timedelta(days=366)) == 0
    assert len((await db_session.scalars(select(CronRun))).all()) == 1


async def test_once_misfire_consumed_without_queue(db_engine, db_session):
    now = datetime.now(UTC)
    row = await job(db_session, now - timedelta(minutes=6), schedule_kind="once", run_at=now)
    factory = make_session_factory(db_engine)
    assert await run_tick(factory, now) == 1
    await db_session.refresh(row)
    assert row.consumed_at == now and not row.enabled and row.next_run_at is None
    assert await run_tick(factory, now + timedelta(days=366)) == 0
    assert await db_session.scalar(select(Task)) is None


async def test_once_manual_immediate_consumes_future_occurrence(db_engine, db_session):
    now = datetime.now(UTC)
    row = await job(
        db_session, now + timedelta(hours=1), schedule_kind="once", run_at=now + timedelta(hours=1)
    )
    row.force_run_at, row.force_run_by = now, row.created_by
    await db_session.commit()
    factory = make_session_factory(db_engine)
    assert await run_tick(factory, now) == 1
    await db_session.refresh(row)
    assert row.consumed_at == now and row.next_run_at is None and row.enabled
    assert await run_tick(factory, now + timedelta(hours=2)) == 0
    assert len((await db_session.scalars(select(Task))).all()) == 1


async def test_once_invalid_actor_skip_consumes_schedule(db_engine, db_session):
    from coreman.core.db.models import User

    now = datetime.now(UTC)
    row = await job(db_session, now, schedule_kind="once", run_at=now)
    creator = await db_session.get(User, row.created_by)
    creator.status = "disabled"
    await db_session.commit()
    assert await run_tick(make_session_factory(db_engine), now) == 1
    await db_session.refresh(row)
    assert row.consumed_at is not None and row.next_run_at is None and not row.enabled
    assert (await db_session.scalar(select(CronRun))).status == "skipped"


async def test_once_missing_next_time_initializes_without_consuming(db_engine, db_session):
    now = datetime.now(UTC)
    row = await job(db_session, None, schedule_kind="once", run_at=now + timedelta(minutes=2))
    factory = make_session_factory(db_engine)
    assert await run_tick(factory, now) == 0
    await db_session.refresh(row)
    assert row.next_run_at == row.run_at and row.consumed_at is None
    assert await run_tick(factory, now + timedelta(minutes=2)) == 1
