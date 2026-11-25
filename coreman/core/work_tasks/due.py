"""截止提醒：快到期（24 小时内）提醒一次，超期再提醒一次；卡片上可以直接延期。

提醒发给手上拿着这件事的人：执行人是同事就发给他，是 AI 员工就发给代表人（AI 员工不看卡片）。
镜像的账本记录不提醒（在原处处理）。
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
