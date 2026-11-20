"""所有者在「我的助理」页上对一条待跟进的三个动作：补充、改期、关闭（选原因）。

每个动作都记一行 `assistant_feedback`：下班编译把窗口里的反馈交给编译子任务提炼进知识库。
补充与改期备注还当场写进这一条的备注（助理下一轮就看得到）；补充另外立刻交回助理重新判断。
所有者原话只进封存列（备注、反馈的 `note_enc`、交回条目的 `summary_enc`），平台写的只有编号与代码。
只 flush，不提交。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, sealed
from coreman.core.assistants.followups import FollowupError
from coreman.core.db.models import (
    FOLLOWUP_OWNER_CLOSE_CODES,
    AssistantFeedback,
    AssistantFollowup,
)
from coreman.core.timeutils import utcnow

MAX_NOTE = 1000
# 交回助理的提醒种类（见 `handover`）。
SUPPLEMENT_REMINDER = "owner_feedback"
# 这几种关闭原因记为「不再需要」，其余记为办完。
DROPPED_CODES = ("not_needed",)
CLOSE_CODES = FOLLOWUP_OWNER_CLOSE_CODES
# 写进备注的前缀：助理据此认出是所有者本人写的。
SUPPLEMENT_PREFIX = "所有者补充："
RESCHEDULE_PREFIX = "所有者改期到 {day}"


def _note(value: str | None, *, required: bool = False) -> str | None:
    text = " ".join((value or "").split())
    if (required and not text) or len(text) > MAX_NOTE:
        raise FollowupError("invalid_feedback")
    return text or None


async def _record(
    session: AsyncSession,
    bot_id: uuid.UUID,
    row: AssistantFollowup,
    kind: str,
    *,
    code: str | None = None,
    note: str | None = None,
) -> None:
    session.add(
        AssistantFeedback(
            bot_id=bot_id, followup_id=row.id, seq=row.seq, kind=kind, code=code, note_enc=note
        )
    )
    await session.flush()


async def supplement(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, text: str, *, today: date
) -> AssistantFollowup:
    """补充一段话：写进备注、清掉停滞原因、记一条反馈，并立刻交回助理重新判断。

    Raises:
        FollowupError: invalid_feedback / notes_full / followup_not_found / followup_closed /
            content_locked
    """
    note = _note(text, required=True)
    assert note is not None
    row = await followups.get(session, bot_id, seq, lock=True)
    if row.status not in followups.LIVE:
        raise FollowupError("followup_closed")
    if not await sealed.key_ready(session, bot_id):
        raise FollowupError("content_locked")
    followups.append_line(row, SUPPLEMENT_PREFIX + note, today)
    # 所有者给了做法或该找的人：不再算停滞，由助理重新判断它该在哪。
    row.stuck = None
    now = utcnow()
    row.updated_at = row.progress_at = now
    await session.flush()
    await _record(session, bot_id, row, "supplement", note=note)
    # 函数内引入：delivery_checks 依赖面大，模块层引入会成环。
    from coreman.core.assistants import delivery_checks

    await delivery_checks.add_reminder(
        session,
        bot_id,
        f"{SUPPLEMENT_REMINDER}:{row.id}:{int(now.timestamp() * 1000)}",
        level="immediate",
        now=now,
        summary={
            "reminder": SUPPLEMENT_REMINDER,
            "label": followups.label(row.seq),
            "title": row.title_enc,
            "text": note,
        },
    )
    return row


async def reschedule(
    session: AsyncSession,
    bot_id: uuid.UUID,
    seq: int,
    until: date,
    note: str | None,
    *,
    today: date,
) -> AssistantFollowup:
    """改期：推到 `until` 那天再启动（进入排期），备注写进这一条、记一条反馈。

    Raises:
        FollowupError: snooze_not_future / invalid_feedback / notes_full / followup_not_found /
            followup_closed / content_locked
    """
    text = _note(note)
    if until <= today:
        raise FollowupError("snooze_not_future")
    row = await followups.get(session, bot_id, seq, lock=True)
    if row.status not in followups.LIVE:
        raise FollowupError("followup_closed")
    if text is not None:
        if not await sealed.key_ready(session, bot_id):
            raise FollowupError("content_locked")
        followups.append_line(
            row, RESCHEDULE_PREFIX.format(day=until.isoformat()) + "：" + text, today
        )
    row = await followups.snooze(session, bot_id, seq, until, today=today)
    row.stuck = None
    await session.flush()
    await _record(session, bot_id, row, "reschedule", code=until.isoformat(), note=text)
    return row


async def close(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, code: str, note: str | None
) -> AssistantFollowup:
    """按所有者选的原因关闭，记一条反馈。已经关了的原样返回，不再记。

    Raises:
        FollowupError: invalid_close_code / invalid_feedback / followup_not_found / content_locked
    """
    if code not in CLOSE_CODES:
        raise FollowupError("invalid_close_code")
    text = _note(note)
    row = await followups.get(session, bot_id, seq, lock=True)
    if row.status not in followups.LIVE:
        return row
    if text is not None and not await sealed.key_ready(session, bot_id):
        raise FollowupError("content_locked")
    row = await followups.close(session, bot_id, seq, dropped=code in DROPPED_CODES, code=code)
    await _record(session, bot_id, row, "close", code=code, note=text)
    return row


@dataclass(frozen=True)
class Entry:
    """窗口里的一条反馈：编号、种类、代码是元数据，`note` 是所有者原话（读不出来时为空）。"""

    label: str
    kind: str
    code: str
    note: str


async def since(
    session: AsyncSession, bot_id: uuid.UUID, start: datetime, limit: int
) -> list[Entry]:
    """`start` 之后的反馈，早的在前，至多 `limit` 条（取最近的）。"""
    rows = list(
        await session.scalars(
            select(AssistantFeedback)
            .where(AssistantFeedback.bot_id == bot_id, AssistantFeedback.created_at > start)
            .order_by(AssistantFeedback.created_at.desc())
            .limit(limit)
        )
    )
    await sealed.open_rows(session, rows)
    out = []
    for row in reversed(rows):
        locked = sealed.is_locked(row)
        out.append(
            Entry(
                label=followups.label(row.seq) if row.seq else "",
                kind=row.kind,
                code=row.code or "",
                note="" if locked else (row.note_enc or ""),
            )
        )
    return out
