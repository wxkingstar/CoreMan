"""盯消息的收件箱：把分好级的条目写进 `assistant_inbox`，以及压着、作废。

消息内容不落库：这里写下的只有定位信息（会话、消息、话题、时间）与加密的摘要。取走与交回
在投递层；条目一旦被它装进一轮（`delivered_task_id` 非空），这里不再改。各函数都不提交。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantInbox

# 摘要里正文最多留这么多字：交回时够助理判断轻重，细节由它自己再读会话。
SUMMARY_TEXT = 500


def summary(item: Item, owner_open_id: str) -> dict[str, Any]:
    """入箱的摘要（整块加密）：谁、在哪种会话里、说了什么的开头、有没有 @ 所有者。"""
    return {
        "chat_kind": item.chat_kind,
        "chat_name": item.chat_name[:100],
        "sender_id": item.sender_id,
        "sender_type": item.sender_type,
        "msg_type": item.msg_type,
        "text": item.text[:SUMMARY_TEXT],
        "mentioned": owner_open_id in item.mentions,
    }


def _open(bot_id: uuid.UUID) -> tuple[Any, ...]:
    """这位助理还没交回、也没被投递层装进一轮的条目。"""
    return (
        AssistantInbox.bot_id == bot_id,
        AssistantInbox.status == "pending",
        AssistantInbox.delivered_task_id.is_(None),
    )


async def batch_until(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> datetime | None:
    """这位助理正在攒的那一批什么时候到点；没有在攒的返回 None。"""
    found: datetime | None = await session.scalar(
        select(func.min(AssistantInbox.hold_until)).where(
            *_open(bot_id),
            AssistantInbox.level == Level.BATCH.value,
            AssistantInbox.hold_until > now,
        )
    )
    return found


async def add(
    session: AsyncSession,
    bot_id: uuid.UUID,
    item: Item,
    level: Level,
    *,
    owner_open_id: str,
    hold_until: datetime | None,
) -> bool:
    """入箱；这条消息已经在箱里（不论什么状态）就什么都不做，返回 False。

    靠 `(bot_id, kind, message_id)` 的唯一索引去重：轮询重跑、两个 scheduler 交接时重叠，
    同一条消息都只有一行。
    """
    values = await sealed.seal_values(
        session,
        AssistantInbox,
        bot_id,
        {
            "bot_id": bot_id,
            "kind": "message",
            "level": level.value,
            "chat_id": item.chat_id,
            "message_id": item.message_id,
            "thread_id": item.thread_id,
            "message_at": item.created_at,
            "summary_enc": summary(item, owner_open_id),
            "hold_until": hold_until,
        },
    )
    inserted = await session.scalar(
        insert(AssistantInbox)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["bot_id", "kind", "message_id"],
            index_where=text("message_id IS NOT NULL"),
        )
        .returning(AssistantInbox.id)
    )
    return inserted is not None


def _count(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def hold_chat(session: AsyncSession, bot_id: uuid.UUID, chat_id: str, until: datetime) -> int:
    """所有者刚在这个会话里亲自发过言：还没交回的条目压到 `until`（只往后推，不提前）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(*_open(bot_id), AssistantInbox.kind == "message", AssistantInbox.chat_id == chat_id)
        .values(
            level=Level.HOLD.value,
            hold_until=func.greatest(func.coalesce(AssistantInbox.hold_until, until), until),
            updated_at=func.now(),
        )
    )
    return _count(result)


async def void_message(session: AsyncSession, bot_id: uuid.UUID, message_id: str, code: str) -> int:
    """这条消息对应的、还没交回的条目作废（对方撤回）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(
            *_open(bot_id),
            AssistantInbox.kind == "message",
            AssistantInbox.message_id == message_id,
        )
        .values(status="void", void_code=code, updated_at=func.now())
    )
    return _count(result)


async def void_all(session: AsyncSession, bot_id: uuid.UUID, code: str) -> int:
    """这位助理还没交回的条目全部作废（停用）。"""
    result = await session.execute(
        update(AssistantInbox)
        .where(*_open(bot_id))
        .values(status="void", void_code=code, updated_at=func.now())
    )
    return _count(result)


async def pending_messages(session: AsyncSession, bot_id: uuid.UUID, limit: int = 20) -> list[str]:
    """还没交回的消息条目的消息 id，新的在前：每轮拿它们去查有没有被撤回。"""
    found = await session.scalars(
        select(AssistantInbox.message_id)
        .where(
            *_open(bot_id), AssistantInbox.kind == "message", AssistantInbox.message_id.is_not(None)
        )
        .order_by(AssistantInbox.id.desc())
        .limit(limit)
    )
    return [message_id for message_id in found if message_id]
