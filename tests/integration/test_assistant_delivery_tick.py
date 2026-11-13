"""投递的循环：先对账再入队；暂停时条目留着，恢复后一并交回；scheduler 单独一条循环。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import delivery
from coreman.core.assistants import delivery_checks as checks
from coreman.core.bus import tasks
from coreman.core.db.models import RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import service
from coreman.runtime.scheduler.service import SchedulerService
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import boxed, handovers, inbox_rows, ready

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_the_loop_settles_finished_turns_then_queues_the_next(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    factory = make_session_factory(db_engine)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    with capture_logs() as logs:
        first = await delivery.tick(factory, now)
    assert first == {"assistant_inbox_settled": 0, "assistant_handovers": 1}
    assert SECRET not in str(logs)
    # 在途：再来多少次都不会多排。
    await boxed(db_session, bot, "om_2")
    assert (await delivery.tick(factory, now))["assistant_handovers"] == 0
    (task_id,) = await handovers(db_session, bot)
    # 那一轮被所有者的消息取消了：这次 tick 对账放回。所有者正在场，放回的那条与期间新来的
    # 那条都至少等一分钟，不马上再开一轮。
    await tasks.finish(db_session, task_id, status="cancelled", error_code="superseded")
    await db_session.commit()
    assert await delivery.tick(factory, now) == {
        "assistant_inbox_settled": 1,
        "assistant_handovers": 0,
    }
    assert [row.delivered_task_id for row in await inbox_rows(db_session, bot)] == [None, None]
    # 等够了：下一轮把两条一起带上。
    assert await delivery.tick(factory, now + timedelta(minutes=2)) == {
        "assistant_inbox_settled": 0,
        "assistant_handovers": 1,
    }
    second = (await handovers(db_session, bot))[-1]
    assert [row.delivered_task_id for row in await inbox_rows(db_session, bot)] == [second] * 2
    # 这一轮正常结束：两条都记为已交回。
    await tasks.finish(db_session, second, status="succeeded")
    await db_session.commit()
    assert await delivery.tick(factory, now + timedelta(minutes=2)) == {
        "assistant_inbox_settled": 2,
        "assistant_handovers": 0,
    }
    rows = await inbox_rows(db_session, bot)
    assert [(row.status, row.delivered_task_id) for row in rows] == [("delivered", second)] * 2


async def test_a_paused_runtime_keeps_items_until_it_is_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    factory = make_session_factory(db_engine)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False
    await db_session.commit()
    outgoing = "SELECT count(*) FROM outbox WHERE bot_id = :bot"
    before = await raw(db_session, outgoing, bot=bot.id)
    await boxed(db_session, bot, "om_1")
    await boxed(db_session, bot, "om_2", level="batch", hold_until=utcnow())
    for _ in range(3):
        assert not any((await delivery.tick(factory, utcnow())).values())
    assert await handovers(db_session, bot) == []
    assert await raw(db_session, outgoing, bot=bot.id) == before
    relay.assistant_pool = True
    await db_session.commit()
    assert (await delivery.tick(factory, utcnow()))["assistant_handovers"] == 1
    (task_id,) = await handovers(db_session, bot)
    assert [row.delivered_task_id for row in await inbox_rows(db_session, bot)] == [task_id] * 2


async def test_one_failing_assistant_only_logs_a_type(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")

    async def broken(*_args: Any) -> None:
        raise RuntimeError(f"synthetic {SECRET}")

    monkeypatch.setattr(delivery, "dispatch", broken)
    with capture_logs() as logs:
        counts = await delivery.tick(make_session_factory(db_engine), utcnow())
    assert counts["assistant_handovers"] == 0
    assert SECRET not in str(logs) and "RuntimeError" in str(logs)


async def test_the_scheduler_runs_delivery_in_its_own_loop_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[datetime] = []
    swept: list[datetime] = []

    async def deliver(_factory: Any, now: datetime) -> dict[str, int]:
        calls.append(now)
        if len(calls) == 1:
            return {"assistant_handovers": 1}
        raise RuntimeError("synthetic")

    async def sweep(_factory: Any, _cipher: Any, now: datetime) -> dict[str, int]:
        swept.append(now)
        return {}

    monkeypatch.setattr(delivery, "tick", deliver)
    monkeypatch.setattr(checks, "tick", sweep)
    monkeypatch.setattr(service, "ASSISTANT_DELIVERY_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.05)
        assert svc.is_leader
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(calls) >= 3 and all(now.tzinfo is not None for now in calls)
    # 投递出错的那几次不做顺带检查，下一轮再来；循环没有停。
    assert len(swept) == 1
    assert any(job.get_name() == "scheduler-assistant-delivery" for job in svc._bg)
