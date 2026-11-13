"""投递层用例的公共件：一位可以投递的助理、收件箱条目、对话记录。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed, settings
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.db.models import AssistantInbox, Bot, ChatLog, Task, User
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET
from tests.integration.test_assistant_platform_turn_handler import assistant


async def ready(session: AsyncSession) -> tuple[Bot, User]:
    """可以投递的助理：开通好、记下了身份、冷启动已完成、不看钟点一直在上班。"""
    bot, owner = await assistant(session)
    prefs = await settings.get(session, bot.id)
    prefs.shift_override = "on"
    prefs.cold_start_done_at = utcnow()
    await session.commit()
    return bot, owner


def turn_at(bot: Bot, at: datetime) -> ChatLog:
    return ChatLog(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=at,
    )


async def spend(session: AsyncSession, bot: Bot, count: int, at: datetime) -> None:
    """记 `count` 轮已经跑过的对话。"""
    session.add_all([turn_at(bot, at) for _ in range(count)])
    await session.commit()


async def boxed(
    session: AsyncSession,
    bot: Bot,
    mid: str,
    *,
    level: str = "immediate",
    hold_until: datetime | None = None,
    chat: str = "oc_li",
    text: str = f"周五能给报价吗？{SECRET}",
    kind: str = "message",
    summary: dict[str, Any] | None = None,
) -> AssistantInbox:
    """一条已经入箱的条目（消息条目的摘要与盯消息写的同形）。"""
    row = AssistantInbox(
        bot_id=bot.id,
        kind=kind,
        level=level,
        chat_id=chat,
        message_id=mid,
        message_at=utcnow(),
        hold_until=hold_until,
        summary_enc=summary
        or {
            "chat_kind": "p2p",
            "chat_name": "李四",
            "sender_id": "ou_li",
            "sender_type": "user",
            "msg_type": "text",
            "text": text,
            "mentioned": False,
        },
    )
    session.add(row)
    await session.commit()
    return row


async def inbox_rows(session: AsyncSession, bot: Bot) -> list[AssistantInbox]:
    found = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot.id)
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, found)
    return found


async def handovers(session: AsyncSession, bot: Bot) -> list[int]:
    return list(
        await session.scalars(
            select(Task.id)
            .where(Task.bot_id == bot.id, Task.kind == HANDOVER_KIND)
            .order_by(Task.id)
        )
    )
