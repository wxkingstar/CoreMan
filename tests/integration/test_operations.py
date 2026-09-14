from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from coreman.core.db.models import ProcessInstance
from coreman.core.db.session import make_session_factory
from coreman.operations import drain, ready
from coreman.runtime.bus import instances


async def test_drain_requires_replacement_and_never_kills_running_instance(db_engine, db_session):
    factory = make_session_factory(db_engine)
    source, target = "worker-a:old:", "worker-b:new:"
    await instances.register(
        db_session, instance_id=source + "1", service="worker", version="old", capacity=1
    )
    await db_session.commit()
    with pytest.raises(ValueError):
        await drain(factory, source, 0.05, target)
    row = await db_session.scalar(select(ProcessInstance))
    assert row.drain_requested_at is None
    await instances.register(
        db_session, instance_id=target + "1", service="worker", version="new", capacity=1
    )
    await db_session.commit()
    assert await ready(factory, target)
    with pytest.raises(TimeoutError):
        await drain(factory, source, 0.05, target)
    await db_session.refresh(row)
    assert row.drain_requested_at is not None and row.stopped_at is None
    row.stopped_at = datetime.now(UTC)
    await db_session.commit()
    await drain(factory, source, 1, target)
