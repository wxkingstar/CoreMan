"""盯消息顺带的检查：每位助理每 5 分钟一次，不经模型。

产出是 `kind = 'reminder'` 的收件箱条目（与消息条目一起交回、一起记账、一起受预算约束），
或直接动作（撤回审批单、私信授权到期提醒）。去重靠收件箱的唯一索引 `(bot_id, kind,
message_id)`：提醒条目的 `message_id` 存的是去重键，真实的消息 id 在摘要里。摘要的形状见
`handover.py`。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, sealed
from coreman.core.db.models import AssistantInbox, AssistantMark
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 「处理中」表情挂了这么久还没回，提醒助理。
MARK_STALE = timedelta(hours=4)
# 一次检查最多为多少条到期的待跟进入箱；多出来的下一次再入。
FOLLOWUP_BATCH = 20


async def add_reminder(
    session: AsyncSession,
    bot_id: uuid.UUID,
    key: str,
    *,
    level: str,
    summary: dict[str, Any],
    now: datetime,
    chat_id: str | None = None,
) -> bool:
    """入一条提醒；同一个 `key` 已经入过（不论现在什么状态）就什么都不做，返回 False。

    Args:
        key: 去重键，存进 `message_id`；带日期的键每天能入一次
        level: `immediate` 或 `batch`。两种都不设 `hold_until`（下一次投递就带上）；区别在
            预算用完之后：`batch` 留到次日
        summary: 提醒的内容，整块加密
    """
    values = await sealed.seal_values(
        session,
        AssistantInbox,
        bot_id,
        {
            "bot_id": bot_id,
            "kind": "reminder",
            "level": level,
            "chat_id": chat_id,
            "message_id": key,
            "message_at": now,
            "summary_enc": summary,
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


async def followups_due(
    session: AsyncSession, bot_id: uuid.UUID, today: date, now: datetime
) -> int:
    """当天到期的待跟进各入一条提醒，每条每天至多一次；返回新入了几条。"""
    added = 0
    for row in (await followups.due(session, bot_id, today))[:FOLLOWUP_BATCH]:
        if sealed.is_locked(row):
            continue
        added += await add_reminder(
            session,
            bot_id,
            f"followup:{row.id}:{today.isoformat()}",
            level="batch",
            now=now,
            summary={
                "reminder": "followup_due",
                "label": followups.label(row.seq),
                "category": row.category,
                "due_on": row.due_on.isoformat() if row.due_on else None,
                "review_on": row.review_on.isoformat() if row.review_on else None,
                "title": row.title_enc,
            },
        )
    return added


async def stale_marks(session: AsyncSession, bot_id: uuid.UUID, today: date, now: datetime) -> int:
    """挂了 4 小时以上的「处理中」表情各入一条提醒，每条每天至多一次；返回新入了几条。"""
    rows = (
        await session.execute(
            select(AssistantMark.message_id, AssistantMark.chat_id, AssistantMark.created_at)
            .where(AssistantMark.bot_id == bot_id, AssistantMark.created_at <= now - MARK_STALE)
            .order_by(AssistantMark.created_at)
        )
    ).all()
    added = 0
    for message_id, chat_id, marked_at in rows:
        added += await add_reminder(
            session,
            bot_id,
            f"mark:{message_id}:{today.isoformat()}",
            level="batch",
            chat_id=chat_id,
            now=now,
            summary={
                "reminder": "mark_stale",
                "chat_id": chat_id,
                "message_id": message_id,
                "marked_at": marked_at.isoformat(),
            },
        )
    return added
