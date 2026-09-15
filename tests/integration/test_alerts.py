from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coreman.core.db.models import AlertState
from coreman.core.observability.alerts import observe


async def test_alert_holdoff_and_recovery_survive_transactions(db_session):
    now = datetime.now(UTC)
    assert not await observe(db_session, "queue", True, "alert_queue", now, delay=300)
    await db_session.commit()
    assert not await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=299), delay=300
    )
    assert await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=300), delay=300
    )
    await db_session.commit()
    assert not await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=330), delay=300
    )
    assert await observe(
        db_session, "queue", False, "alert_queue", now + timedelta(seconds=360), delay=300
    )
    await db_session.commit()
    row = await db_session.scalar(select(AlertState))
    assert not row.active and not row.firing and row.generation == 1


async def test_evaluation_ignores_future_queue_and_detects_enabled_disconnected_bot(db_session):
    from coreman.core.bus import tasks
    from coreman.core.bus.tasks import NewTask
    from coreman.core.observability.alerts import tick
    from tests.integration.test_scheduler_reaper import _bot

    bot = await _bot(db_session)
    now = datetime.now(UTC)
    await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", run_after=now + timedelta(days=1))
    )
    await db_session.commit()
    await tick(db_session, now)
    await db_session.commit()
    queue = await db_session.get(AlertState, "queue")
    gateway = await db_session.get(AlertState, f"gateway:{bot.id}")
    assert not queue.active and gateway.active and not gateway.firing
    await tick(db_session, now + timedelta(seconds=301))
    await db_session.commit()
    await db_session.refresh(gateway)
    assert gateway.firing
