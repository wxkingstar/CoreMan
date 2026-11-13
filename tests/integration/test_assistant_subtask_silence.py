"""子任务不往所有者的私聊里发任何东西：网关不推它的流，看护器不推进度，收尸与流恢复不补发。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import streams, tasks
from coreman.core.db.models import AssistantJob, Bot, OutboxItem, Task, TaskStream
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler import reaper
from coreman.runtime.worker.background import TimeoutSupervisor
from tests.integration.assistant_helpers import raw
from tests.integration.job_helpers import owner_with_key, round_task, spawned
from tests.integration.test_assistant_sealed_outbox import _transport
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")
CONTEXT = {"chat_id": "oc_private", "chat_type": "single", "_silent": True}


async def outbox_rows(session: AsyncSession, bot: Bot) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(OutboxItem).where(OutboxItem.bot_id == bot.id)
        )
        or 0
    )


async def with_stream(session: AsyncSession, **kw: Any) -> tuple[Bot, AssistantJob, Task]:
    """一个在跑的子任务轮次，流已经开了、写了半截。"""
    bot, _ = await owner_with_key(session)
    row = await spawned(session, bot)
    task = await round_task(session, row)
    await tasks.start(session, task.id)
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s-demo",
        reply_context=CONTEXT,
        lease_generation=1,
        running_since=utcnow(),
        **kw,
    )
    await streams.update(session, task.id, pending_text="查到一半", bot_id=bot.id)
    await session.commit()
    return bot, row, task


async def test_the_gateway_never_pushes_a_silent_stream(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, task = await with_stream(db_session)
    transport, api = await _transport(db_session, db_engine, bot)
    row = await db_session.get(TaskStream, task.id, populate_existing=True)
    assert row is not None
    await transport.push(row)
    assert api.calls == []
    await streams.complete(db_session, task.id, final_text="结论：报价 3 万", bot_id=bot.id)
    await db_session.commit()
    row = await db_session.get(TaskStream, task.id, populate_existing=True)
    assert row is not None and row.is_complete
    await transport.push(row)
    assert api.calls == [] and await outbox_rows(db_session, bot) == 0
    pushed = await raw(
        db_session,
        "SELECT finish_pushed_at IS NOT NULL FROM task_streams WHERE task_id = :id",
        id=task.id,
    )
    assert pushed is True


async def test_a_silent_supervisor_pushes_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, task = await with_stream(db_session)
    supervisor = TimeoutSupervisor(
        build_ctx(db_engine, task),
        cast(Any, None),
        agent_timeout=None,
        chat_id="oc_private",
        platform="feishu",
    )
    supervisor.silent = True
    await supervisor.push("还在处理中")
    await supervisor.on_expired()
    assert await outbox_rows(db_session, bot) == 0
    # 普通轮次不受影响。
    supervisor.silent = False
    await supervisor.push("还在处理中")
    assert await outbox_rows(db_session, bot) == 1


async def test_a_lost_subtask_round_is_not_announced(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """worker 没了：子任务这一轮没有开流，收尸不能往「会话键」发一条——那不是一个会话。"""
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    task = await round_task(db_session, row)
    await tasks.start(db_session, task.id)
    now = utcnow()
    task.heartbeat_at = now - timedelta(seconds=61)
    await db_session.commit()
    lost = await reaper.reap_lost_tasks(
        db_session, now, chat_logs_factory=make_session_factory(db_engine)
    )
    await db_session.commit()
    assert lost == 1 and await outbox_rows(db_session, bot) == 0
    # 补写的那条记录不算「所有者在这个会话里说过话」：子任务的会话键不是一个能推送的会话。
    assert (
        await raw(db_session, "SELECT count(*) FROM chat_logs WHERE task_id = :id", id=task.id) == 1
    )
    assert (
        await raw(db_session, "SELECT count(*) FROM user_reached WHERE bot_id = :id", id=bot.id)
        == 0
    )


async def test_a_recovered_silent_stream_is_not_sent_either(db_session: AsyncSession) -> None:
    """流被网关改成了主动模式、任务已经结束：恢复时照样补上终态，但不往私聊补发。"""
    bot, _, task = await with_stream(
        db_session, delivery_mode="proactive", background_state={"mode": "final_only", "offset": 0}
    )
    await tasks.finish(db_session, task.id, status="failed", error_code="RelayError")
    await db_session.commit()
    later = utcnow() + timedelta(seconds=120)
    assert await reaper.recover_terminal_streams(db_session, later) == 1
    await db_session.commit()
    row = await db_session.get(TaskStream, task.id, populate_existing=True)
    assert row is not None and row.is_complete
    assert await outbox_rows(db_session, bot) == 0
