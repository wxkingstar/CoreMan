from prometheus_client.parser import text_string_to_metric_families

from coreman.core.db.session import make_session_factory
from coreman.core.observability.metrics import render
from coreman.runtime.bus import tasks
from coreman.runtime.bus.tasks import NewTask
from tests.integration.test_scheduler_reaper import _bot


async def test_metrics_aggregate_queue_without_message_or_user_labels(db_session, db_engine):
    bot = await _bot(db_session)
    await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={"secret": "private-message"})
    )
    await db_session.commit()
    body = (await render(make_session_factory(db_engine))).decode()
    samples = {
        sample.name + str(sample.labels): sample.value
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    }
    assert samples["coreman_tasks_queued{'lane': 'normal'}"] == 1
    assert samples["coreman_tasks_queued{'lane': 'fast'}"] == 0
    assert "private-message" not in body and str(bot.id) not in body


async def test_transaction_metrics_discard_rollback(db_session):
    from coreman.core.observability.metrics import after_commit

    calls = []
    after_commit(db_session, lambda: calls.append("committed"))
    await db_session.execute(__import__("sqlalchemy").text("SELECT 1"))
    await db_session.rollback()
    await db_session.execute(__import__("sqlalchemy").text("SELECT 1"))
    await db_session.commit()
    assert calls == []
    after_commit(db_session, lambda: calls.append("committed"))
    await db_session.execute(__import__("sqlalchemy").text("SELECT 1"))
    await db_session.commit()
    assert calls == ["committed"]
