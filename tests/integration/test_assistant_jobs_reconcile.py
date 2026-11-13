"""子任务的对账：超时、没走到收尾的轮次、被所有者的消息取消的投回——都由循环收口，且只投到一次。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import delivery, jobs
from coreman.core.assistants import delivery_checks as checks
from coreman.core.assistants.kinds import JOB_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import RelayServer, Task
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import service
from coreman.runtime.scheduler.service import SchedulerService
from coreman.runtime.worker.assistant_jobs import AssistantJobResultHandler, AssistantSubtaskHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.job_helpers import reloaded, round_task, spawned, take, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant, run
from tests.integration.test_assistant_platform_turn_yield import fresh, owner_message
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
NOTHING = {"assistant_jobs_timed_out": 0, "assistant_jobs_lost": 0, "assistant_job_results": 0}


async def test_a_job_past_its_deadline_fails_and_says_so(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    row = await spawned(db_session, bot, now=now - jobs.LIFETIME - timedelta(minutes=1))
    turn = await round_task(db_session, row)
    counts = await jobs.reconcile(db_session, now)
    await db_session.commit()
    assert counts == {**NOTHING, "assistant_jobs_timed_out": 1, "assistant_job_results": 1}
    failed = await reloaded(db_session, row)
    assert (failed.status, failed.error_code) == ("failed", jobs.TIMEOUT)
    # 在途的那一轮被叫停；它收尾时不再改这个子任务。
    assert (await fresh(db_session, turn)).cancel_requested_at is not None
    assert (
        await jobs.complete(db_session, bot, row.id, task_id=turn.id, ok=True, result="晚了")
        is None
    )
    result = await take(db_session, failed.result_task_id)
    _, relay = await run(db_engine, db_session, result, AssistantJobResultHandler())
    assert "状态：失败（job_timeout）" in user_input(relay.requests[0])
    # 没到点的不动。
    assert await jobs.reconcile(db_session, now) == NOTHING


async def test_rounds_that_never_reached_the_closing_hook_fail_the_job(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    paused = await spawned(db_session, bot, title="暂停时开不了的")
    unsettled = await spawned(db_session, bot, title="成功了却没收尾的")
    purged = await spawned(db_session, bot, title="任务行已被清理的")
    assert paused.last_task_id and unsettled.last_task_id and purged.last_task_id
    await tasks.finish(
        db_session, paused.last_task_id, status="failed", error_code="assistant_paused"
    )
    await tasks.finish(db_session, unsettled.last_task_id, status="succeeded")
    await db_session.execute(delete(Task).where(Task.id == purged.last_task_id))
    await db_session.commit()
    # 刚结束的留给收尾挂钩；任务行不在了的不用等。
    first = await jobs.reconcile(db_session, now)
    await db_session.commit()
    assert first["assistant_jobs_lost"] == 1
    assert (await reloaded(db_session, purged)).error_code == jobs.LOST
    assert (await reloaded(db_session, paused)).status == "queued"
    second = await jobs.reconcile(db_session, now + jobs.LOST_GRACE + timedelta(seconds=60))
    await db_session.commit()
    assert second["assistant_jobs_lost"] == 2
    assert (await reloaded(db_session, paused)).error_code == "assistant_paused"
    late = await reloaded(db_session, unsettled)
    assert (late.status, late.error_code) == ("failed", jobs.LOST)
    # 三个都投回了主会话，各一轮。
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 3


async def test_a_result_cancelled_by_the_owners_message_arrives_later_exactly_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    row = await spawned(db_session, bot)
    await run(db_engine, db_session, await round_task(db_session, row), AssistantSubtaskHandler())
    row = await reloaded(db_session, row)
    assert row.status == "done"
    first = await take(db_session, row.result_task_id)
    # 所有者发来一条：chat 轮次把在跑的结果轮次标成被替代。
    message = await owner_message(db_session, bot)
    handler = ChatTaskHandler()
    handler.SUPERSEDE_WAIT_SECONDS = 0.3
    chat_relay = FakeRelay("normal")
    ctx = build_ctx(db_engine, message, relay_client_factory=lambda _r: chat_relay.client())
    await handler.run(ctx)
    assert (await fresh(db_session, first)).cancel_reason == "superseded"
    done, relay = await run(
        db_engine, db_session, await fresh(db_session, first), AssistantJobResultHandler()
    )
    assert done.status == "cancelled" and relay.requests == []
    # 没走到收尾：对账把它放回，但不马上重投（所有者正在聊）。
    now = utcnow()
    counts = await jobs.reconcile(db_session, now)
    await db_session.commit()
    assert counts == NOTHING
    released = await reloaded(db_session, row)
    assert released.result_task_id is None and released.reported_at is None
    assert released.report_failures == 0
    await tasks.finish(db_session, message.id, status="succeeded")
    await db_session.commit()
    counts = await jobs.reconcile(db_session, now + timedelta(seconds=61))
    await db_session.commit()
    assert counts == {**NOTHING, "assistant_job_results": 1}
    second = await take(db_session, (await reloaded(db_session, row)).result_task_id)
    assert second.id != first.id
    done, relay = await run(db_engine, db_session, second, AssistantJobResultHandler())
    assert done.status == "succeeded"
    assert user_input(relay.requests[0]).startswith(jobs.RESULT_HEADER)
    # 之后无论对多少次账都不会再投。
    for seconds in (62, 600, 7200):
        assert await jobs.reconcile(db_session, now + timedelta(seconds=seconds)) == NOTHING
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 2
    assert (await reloaded(db_session, row)).reported_at is not None


async def test_a_result_turn_that_succeeded_without_its_hook_is_still_counted(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    row = await spawned(db_session, bot)
    assert row.last_task_id is not None
    await tasks.finish(db_session, row.last_task_id, status="succeeded")
    done = await jobs.complete(
        db_session, bot, row.id, task_id=row.last_task_id, ok=True, result="报价 3 万"
    )
    assert done is not None and done.result_task_id is not None
    await tasks.finish(db_session, done.result_task_id, status="succeeded")
    await db_session.commit()
    assert await jobs.reconcile(db_session, utcnow()) == NOTHING
    await db_session.commit()
    assert (await reloaded(db_session, row)).reported_at is not None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1


async def test_a_paused_or_stopping_assistant_gets_no_result_turn(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    row = await spawned(db_session, bot, now=now - jobs.LIFETIME - timedelta(minutes=1))
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False
    await db_session.commit()
    counts = await jobs.reconcile(db_session, now)
    await db_session.commit()
    assert counts == {**NOTHING, "assistant_jobs_timed_out": 1}
    relay.assistant_pool = True
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    assert await jobs.reconcile(db_session, now + timedelta(minutes=5)) == NOTHING
    assert await tasks_of(db_session, bot, JOB_RESULT_KIND) == []
    assert (await reloaded(db_session, row)).status == "failed"


async def test_tick_commits_and_the_scheduler_calls_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    row = await spawned(db_session, bot, now=now - jobs.LIFETIME - timedelta(minutes=1))
    counts = await jobs.tick(make_session_factory(db_engine), now)
    assert counts["assistant_jobs_timed_out"] == 1
    assert (await reloaded(db_session, row)).status == "failed"

    calls: list[datetime] = []
    real = jobs.tick

    async def reconciled(factory: Any, at: datetime) -> dict[str, int]:
        calls.append(at)
        return await real(factory, at)

    async def nothing(*_args: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(jobs, "tick", reconciled)
    monkeypatch.setattr(delivery, "tick", nothing)
    monkeypatch.setattr(checks, "tick", nothing)
    monkeypatch.setattr(service, "ASSISTANT_DELIVERY_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    job = asyncio.create_task(svc.run())
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(job, 10)
    assert len(calls) >= 2 and all(at.tzinfo is not None for at in calls)
