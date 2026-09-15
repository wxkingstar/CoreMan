"""实例被误判死亡后，心跳让它恢复存活（宿主机休眠、数据库抖动之后）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman import operations
from coreman.core.bus import instances
from coreman.core.db.models import ProcessInstance
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler import reaper

PREFIX = "worker-a:revived:"
INSTANCE_ID = f"{PREFIX}1:1"


async def test_heartbeat_revives_an_instance_marked_dead_while_it_slept(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await instances.register(
        db_session, instance_id=INSTANCE_ID, service="worker", version="test", capacity=30
    )
    await db_session.execute(
        update(ProcessInstance)
        .where(ProcessInstance.id == INSTANCE_ID)
        .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    await db_session.commit()
    assert await reaper.mark_dead_instances(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    factory = make_session_factory(db_engine)
    assert await operations.ready(factory, PREFIX) is False
    assert await instances.request_drain(db_session, INSTANCE_ID) is False

    # 进程醒来后的第一次心跳：实例恢复存活，就绪检查与排空请求重新生效。
    assert await instances.heartbeat(db_session, INSTANCE_ID, running=0) is False
    await db_session.commit()
    row = await db_session.get(ProcessInstance, INSTANCE_ID, populate_existing=True)
    assert row is not None and row.stopped_at is None
    assert await operations.ready(factory, PREFIX) is True
    assert await instances.request_drain(db_session, INSTANCE_ID) is True
    await db_session.commit()


async def test_clean_shutdown_stays_stopped(db_session: AsyncSession) -> None:
    await instances.register(
        db_session, instance_id=INSTANCE_ID, service="worker", version="test", capacity=30
    )
    await instances.heartbeat(db_session, INSTANCE_ID, running=0)
    await instances.mark_stopped(db_session, INSTANCE_ID)
    await db_session.commit()
    row = await db_session.get(ProcessInstance, INSTANCE_ID, populate_existing=True)
    assert row is not None and row.stopped_at is not None
