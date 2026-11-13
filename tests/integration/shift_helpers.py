"""上下班、下班编译与冷启动用例的公共件。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sealed, settings
from coreman.core.db.models import AssistantSettings, Bot, OutboxItem, Task, User
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx


async def say(
    engine: AsyncEngine,
    session: AsyncSession,
    bot: Bot,
    text: str,
    *,
    sender: str = "owner_pid",
    chat_type: str = "single",
    chat_id: str = "oc_private",
) -> tuple[Task, FakeRelay]:
    """这位发言人发来一条消息，这一轮跑完：返回任务与模型替身（看有没有派给模型）。"""
    task = await chat_task(session, bot, text, sender=sender, chat_type=chat_type, chat_id=chat_id)
    task.status = "running"
    await session.commit()
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def last_sent(session: AsyncSession, bot: Bot) -> OutboxItem | None:
    """这条记录最近一条要发出去的消息；`session.scalar` 取整行，内容已还原。"""
    found: OutboxItem | None = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
        .order_by(OutboxItem.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return found


async def working(
    session: AsyncSession, *, shift: str | None = "on"
) -> tuple[Bot, User, AssistantSettings]:
    """冷启动已做完、记下了身份、所在节点可用的助理。

    `shift` 写进 `shift_override`、不带到期时刻：用例不依赖跑的时候是星期几、几点。
    """
    bot, owner = await assistant(session)
    prefs = await settings.get(session, bot.id)
    prefs.cold_start_started_at = utcnow() - timedelta(days=1)
    prefs.cold_start_done_at = prefs.cold_start_started_at
    prefs.cold_start_outcome = "completed"
    prefs.shift_override = shift
    await session.commit()
    return bot, owner, prefs


async def tasks_of(session: AsyncSession, bot: Bot, kind: str) -> list[Task]:
    rows = list(
        await session.scalars(
            select(Task)
            .where(Task.bot_id == bot.id, Task.kind == kind)
            .order_by(Task.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows
