"""子任务用例的公共件：助理、派出的子任务、它在途的那一轮。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs, keys, sealed
from coreman.core.bus import instances, tasks
from coreman.core.db.models import AssistantJob, Bot, Task, User
from tests.integration.assistant_helpers import SECRET, identified, seed_assistant

TITLE = "查李四的报价"
BRIEF = f"翻一下上周和李四的聊天，找出他报的价格。{SECRET}"


async def owner_with_key(
    session: AsyncSession,
    *,
    chat_id: str = "oc_private",
    platform_user_id: str = "owner_pid",
    open_id: str = "ou_owner",
) -> tuple[Bot, User]:
    """只有记录、数据密钥与身份的助理（没有节点）：够跑 jobs 的核心函数，开不了轮。"""
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await identified(
        session, bot, chat_id=chat_id, platform_user_id=platform_user_id, open_id=open_id
    )
    return bot, owner


async def spawned(
    session: AsyncSession, bot: Bot, *, title: str = TITLE, brief: str = BRIEF, **kw: Any
) -> AssistantJob:
    row = await jobs.spawn(session, bot, title=title, brief=brief, **kw)
    await session.commit()
    return row


async def reloaded(session: AsyncSession, row: AssistantJob) -> AssistantJob:
    found = await session.get(AssistantJob, row.id, populate_existing=True)
    assert found is not None
    return found


async def take(session: AsyncSession, task_id: int | None) -> Task:
    """把指定的排队任务认领下来（不走认领语句的排序：同一位助理可能还有别的任务在排队）。"""
    assert task_id is not None
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "queued")
        .values(
            status="claimed",
            claimed_by="worker-test",
            claimed_at=func.now(),
            heartbeat_at=func.now(),
            attempts=Task.attempts + 1,
        )
    )
    await session.commit()
    task = await tasks.get(session, task_id)
    assert task is not None and task.status == "claimed"
    return task


async def round_task(session: AsyncSession, row: AssistantJob) -> Task:
    """认领这个子任务在途的那一轮。"""
    return await take(session, (await reloaded(session, row)).last_task_id)


async def tasks_of(session: AsyncSession, bot: Bot, kind: str) -> list[Task]:
    """这位助理某一类的全部任务，按编号升序（只看元数据，载荷没有还原）。"""
    return list(
        await session.scalars(
            select(Task)
            .where(Task.bot_id == bot.id, Task.kind == kind)
            .order_by(Task.id)
            .execution_options(populate_existing=True)
        )
    )
