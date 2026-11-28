"""截止提醒：快到期（24 小时内）提醒一次，超期再提醒一次；卡片上可以直接延期。

提醒发给手上拿着这件事的人：执行人是同事就发给他，是 AI 员工就发给代表人（AI 员工不看卡片）。
镜像的账本记录不提醒（在原处处理）。

另有一种提醒：AI 员工问人的问题发在群里（见 `cards`）之后一直没人答，过了
`ASK_REMIND_AFTER` 在那张卡下面 @ 被问的人说一次「还在等你定」。只提醒一次；事情停在等人
回答上，群里的人不该靠别人去催。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks.service import feishu_user_id
from coreman.core.bus import outbox
from coreman.core.db.models import WORK_TASK_OPEN, WorkTask
from coreman.core.work_tasks import cards
from coreman.core.work_tasks.service import add_event, clip

SOON = timedelta(hours=24)
# 群里的提问卡发出后多久没人答就提醒一次；太久以前的不再补提醒。
ASK_REMIND_AFTER = timedelta(minutes=10)
ASK_REMIND_WINDOW = timedelta(hours=24)
BATCH = 100
EVENT_KEY = "work_task_due"
POSTPONE_DAYS = (1, 3)


def holder(row: WorkTask) -> uuid.UUID | None:
    return row.assignee_user_id or row.principal_user_id or row.requester_user_id


def due_card(row: WorkTask, line: str, base_url: str) -> dict[str, Any]:
    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"延期 {days} 天"},
            "type": "default",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": cards.card_task_id(row),
                        "event_key": EVENT_KEY,
                        "level": f"postpone:{days}",
                    },
                }
            ],
        }
        for days in POSTPONE_DAYS
    ]
    return {
        "schema": "2.0",
        "task_id": cards.card_task_id(row),
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": clip(f"{row.code} {row.title}", 60)},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": line}},
                *buttons,
                {
                    "tag": "markdown",
                    "content": f"[在任务页查看 {row.code}]({cards.task_url(base_url, row.code)})",
                },
            ]
        },
    }


async def _remind(session: AsyncSession, row: WorkTask, line: str, key: str, base: str) -> None:
    bot = await cards.sender(session, row)
    to = holder(row)
    pid = await feishu_user_id(session, to) if to else None
    if bot is not None and pid:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"work-task:{row.id}:{key}:{row.due_at.isoformat() if row.due_at else ''}",
            target={"user_id": pid},
            payload={"card": due_card(row, line, base)},
        )
    add_event(session, row, kind=key, actor_type="system", actor_id=None, human_text=line)


async def tick(session: AsyncSession, now: datetime, base_url: str) -> int:
    rows = list(
        await session.scalars(
            select(WorkTask)
            .where(
                WorkTask.due_at.is_not(None),
                WorkTask.due_at <= now + SOON,
                WorkTask.status.in_(WORK_TASK_OPEN),
                WorkTask.origin_ref.is_(None),
                WorkTask.overdue_notified_at.is_(None),
                # 已经提醒过「快到期」、还没超期的不占名额。
                or_(WorkTask.due_at <= now, WorkTask.due_reminded_at.is_(None)),
            )
            .order_by(WorkTask.due_at)
            .limit(BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    sent = 0
    for row in rows:
        assert row.due_at is not None
        if row.due_at <= now:
            row.overdue_notified_at = now
            await _remind(
                session, row, f"已超期：截止 {row.due_at:%m-%d %H:%M} UTC", "overdue", base_url
            )
            sent += 1
        elif row.due_reminded_at is None:
            row.due_reminded_at = now
            await _remind(
                session, row, f"快到期：截止 {row.due_at:%m-%d %H:%M} UTC", "due_soon", base_url
            )
            sent += 1
    return sent


def postpone(row: WorkTask, days: int, now: datetime) -> None:
    base = row.due_at if row.due_at and row.due_at > now else now
    row.due_at = base + timedelta(days=days)
    row.due_reminded_at = row.overdue_notified_at = None


async def remind_asks(session: AsyncSession, now: datetime) -> int:
    """群里的提问卡没人答：在卡片下面 @ 被问的人提醒一次。返回这一轮提醒了几个。"""
    from coreman.core.db.models import OutboxItem
    from coreman.core.work_tasks import progress

    rows = list(
        await session.scalars(
            select(WorkTask)
            .where(
                WorkTask.ask.is_not(None),
                WorkTask.assignee_user_id.is_not(None),
                WorkTask.brief.like(f"{progress.ASK_PREFIX}%"),
                WorkTask.status.in_(WORK_TASK_OPEN),
                WorkTask.created_at <= now - ASK_REMIND_AFTER,
                WorkTask.created_at >= now - ASK_REMIND_WINDOW,
            )
            .order_by(WorkTask.id)
            .limit(BATCH)
        )
    )
    sent = 0
    for row in rows:
        ask = row.ask or {}
        key = f"work-task:{row.id}:ask:{ask.get('id')}"
        card = await session.scalar(
            select(OutboxItem).where(OutboxItem.dedupe_key == key, OutboxItem.status == "sent")
        )
        message_id = (card.payload or {}).get("_feishu_message_id") if card is not None else None
        chat_id = (card.target or {}).get("chat_id") if card is not None else None
        # 只提醒发在群里的卡：私聊的卡对方自己看得到，不再追一条。
        if card is None or not message_id or not chat_id:
            continue
        to = ask.get("to_user_id")
        pid = await feishu_user_id(session, uuid.UUID(to)) if to else None
        if not pid:
            continue
        item = await outbox.add(
            session,
            bot_id=card.bot_id,
            platform="feishu",
            kind="send",
            dedupe_key=f"{key}:remind",
            target={"chat_id": chat_id, "message_id": message_id},
            payload={
                "markdown": f"这件事还在等你定：{clip(str(ask.get('question') or ''), 80)}",
                "_mention_user_id": pid,
            },
        )
        if item is not None:
            add_event(
                session,
                row,
                kind="ask_reminded",
                actor_type="system",
                actor_id=None,
                human_text="过了 10 分钟没人回答，在群里提醒了一次",
            )
            sent += 1
    return sent
