"""上下班、下班编译与冷启动用例的公共件。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import Bot, OutboxItem, Task
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
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
