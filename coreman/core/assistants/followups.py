"""个人助理的待跟进：助理替所有者记着的跨天的事，按助理编号（F012）。

标题与备注经 ORM 读写，由 `sealed` 的 flush 钩子封存、会话取出时还原；分类、状态、日期、编号
是明文。所有函数按 `bot_id` 过滤：一位助理看不到也改不了别人的条目。只 flush，不提交。

经 ORM 写入不会签发数据密钥，取不到密钥时 flush 钩子只会把内容列写成空：所以凡是要写标题或
备注的地方先确认密钥可用（`_writable`），不可用就报 `content_locked`，不静默丢内容。只动元数据
的操作（改期、改类、推后、关闭）不需要密钥。
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.db.models import FOLLOWUP_CATEGORIES, AssistantFollowup
from coreman.core.timeutils import utcnow

# 每位助理未结（open + snoozed）条目的上限。
MAX_OPEN = 500
MAX_TITLE = 200
MAX_NOTES = 4000
LIVE = ("open", "snoozed")
SCOPES = ("today", "open", "all", *FOLLOWUP_CATEGORIES)
# 平台自动关闭的原因：起因消息被撤回。
RECALLED = "origin_recalled"
_LABEL = re.compile(r"^[Ff]?(\d{1,9})$")


class Keep(enum.Enum):
    KEEP = "keep"


# 「不改这一项」：与 None（清空）区分开。
KEEP = Keep.KEEP


class FollowupError(Exception):
    """调用方能照着改的拒绝；`code` 原样作为工具的 error 返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def label(seq: int) -> str:
    return f"F{seq:03d}"


def parse_label(value: str) -> int:
    match = _LABEL.match(value.strip())
    if match is None or int(match.group(1)) < 1:
        raise FollowupError("followup_not_found")
    return int(match.group(1))


def view(row: AssistantFollowup, *, notes: bool = True) -> dict[str, Any]:
    """给助理（以及之后的页面）看的一条；`notes` 为假时只说有没有备注。"""
    out: dict[str, Any] = {
        "id": label(row.seq),
        "category": row.category,
        "status": row.status,
        "title": row.title_enc,
        "review_on": row.review_on.isoformat() if row.review_on else None,
        "due_on": row.due_on.isoformat() if row.due_on else None,
        "origin_message_id": row.origin_message_id,
        "needs_owner": row.needs_owner,
        "created_at": row.created_at.isoformat(),
    }
    if notes:
        out["notes"] = row.notes_enc
    else:
        out["has_notes"] = bool(row.notes_enc)
    if row.close_code:
        out["close_code"] = row.close_code
    if sealed.is_locked(row):
        out["locked"] = True
    return out


def _title(value: str) -> str:
    title = value.strip()
    if not title or len(title) > MAX_TITLE:
        raise FollowupError("invalid_title")
    return title


def _notes(value: str | None) -> str | None:
    if value is not None and len(value) > MAX_NOTES:
        raise FollowupError("invalid_notes")
    return value or None


def _category(value: str) -> str:
    if value not in FOLLOWUP_CATEGORIES:
        raise FollowupError("invalid_category")
    return value


async def _lock(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """这位助理的编号与上限在本事务内独占；不锁 bots 的行，那一行别处一直在更新。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assistant-followups:{bot_id}"},
    )


async def _writable(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """要写标题或备注：所有者的数据密钥必须可用（存在、没销毁、解得开）。

    Raises:
        FollowupError: content_locked
    """
    if not await sealed.key_ready(session, bot_id):
        raise FollowupError("content_locked")


async def add(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    category: str,
    title: str,
    notes: str | None = None,
    review_on: date | None = None,
    due_on: date | None = None,
    origin_message_id: str | None = None,
    job_id: uuid.UUID | None = None,
    needs_owner: bool = False,
) -> AssistantFollowup:
    """记一条，编号取这位助理的下一个。

    Args:
        needs_owner: 要所有者在场才办得了：摘要里逐条列出，带「现在处理」按钮

    Raises:
        FollowupError: invalid_category / invalid_title / invalid_notes / too_many_followups /
            content_locked
    """
    category, title, notes = _category(category), _title(title), _notes(notes)
    await _writable(session, bot_id)
    await _lock(session, bot_id)
    live = await session.scalar(
        select(func.count())
        .select_from(AssistantFollowup)
        .where(AssistantFollowup.bot_id == bot_id, AssistantFollowup.status.in_(LIVE))
    )
    if (live or 0) >= MAX_OPEN:
        raise FollowupError("too_many_followups")
    last = await session.scalar(
        select(func.max(AssistantFollowup.seq)).where(AssistantFollowup.bot_id == bot_id)
    )
    row = AssistantFollowup(
        bot_id=bot_id,
        seq=(last or 0) + 1,
        category=category,
        status="open",
        title_enc=title,
        notes_enc=notes,
        review_on=review_on,
        due_on=due_on,
        origin_message_id=origin_message_id,
        job_id=job_id,
        needs_owner=needs_owner,
    )
    session.add(row)
    await session.flush()
    return row


async def get(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, *, lock: bool = False
) -> AssistantFollowup:
    """这位助理的第 `seq` 条，内容已还原。

    Raises:
        FollowupError: followup_not_found
    """
    stmt = select(AssistantFollowup).where(
        AssistantFollowup.bot_id == bot_id, AssistantFollowup.seq == seq
    )
    if lock:
        stmt = stmt.with_for_update()
    # session.scalar 取单个实体：SealedSession 自动还原内容列。
    row: AssistantFollowup | None = await session.scalar(stmt)
    if row is None:
        raise FollowupError("followup_not_found")
    return row


async def _live(session: AsyncSession, bot_id: uuid.UUID, seq: int) -> AssistantFollowup:
    row = await get(session, bot_id, seq, lock=True)
    if row.status not in LIVE:
        raise FollowupError("followup_closed")
    return row


async def update(
    session: AsyncSession,
    bot_id: uuid.UUID,
    seq: int,
    *,
    category: str | None = None,
    title: str | None = None,
    notes: str | None | Keep = KEEP,
    review_on: date | None | Keep = KEEP,
    due_on: date | None | Keep = KEEP,
    needs_owner: bool | None = None,
) -> AssistantFollowup:
    """改一条未结的；没给的项不动，`notes` / `review_on` / `due_on` 给 None 是清空。

    Raises:
        FollowupError: followup_not_found / followup_closed / invalid_* / content_locked
    """
    row = await _live(session, bot_id, seq)
    if title is not None or not isinstance(notes, Keep):
        await _writable(session, bot_id)
    if category is not None:
        row.category = _category(category)
    if title is not None:
        row.title_enc = _title(title)
    if not isinstance(notes, Keep):
        row.notes_enc = _notes(notes)
    if not isinstance(due_on, Keep):
        row.due_on = due_on
    if needs_owner is not None:
        row.needs_owner = needs_owner
    if not isinstance(review_on, Keep):
        row.review_on = review_on
        if review_on is None and row.status == "snoozed":
            # 没有回看日期的推后条目永远不会醒。
            row.status = "open"
    row.updated_at = utcnow()
    await session.flush()
    return row


async def snooze(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, until: date, *, today: date
) -> AssistantFollowup:
    """推到 `until` 那天再看；到那天由 `due` / `list_` 改回未结。

    Args:
        today: 所有者时区的今天

    Raises:
        FollowupError: snooze_not_future / followup_not_found / followup_closed
    """
    if until <= today:
        raise FollowupError("snooze_not_future")
    row = await _live(session, bot_id, seq)
    row.status, row.review_on, row.updated_at = "snoozed", until, utcnow()
    await session.flush()
    return row


async def close(
    session: AsyncSession,
    bot_id: uuid.UUID,
    seq: int,
    *,
    dropped: bool = False,
    notes: str | None = None,
) -> AssistantFollowup:
    """办完（或 `dropped`：不再需要）。已经关了的原样返回。

    Args:
        notes: 给了就替换备注（怎么了结的）

    Raises:
        FollowupError: followup_not_found / invalid_notes / content_locked
    """
    row = await get(session, bot_id, seq, lock=True)
    if row.status not in LIVE:
        return row
    if notes is not None:
        await _writable(session, bot_id)
        row.notes_enc = _notes(notes)
    row.status = "dropped" if dropped else "done"
    row.closed_at = row.updated_at = utcnow()
    await session.flush()
    if row.needs_owner:
        await _retire(session, bot_id, [row.seq])
    return row


async def _retire(session: AsyncSession, bot_id: uuid.UUID, seqs: list[int]) -> None:
    """关掉的要当面处理的条目：摘要卡片上它的「现在处理」按钮置灰。"""
    # 函数内引入：digest 在模块层引用本模块。
    from coreman.core.assistants import digest

    await digest.retire(session, bot_id, seqs)


async def _wake(session: AsyncSession, bot_id: uuid.UUID, today: date) -> None:
    """推后的条目到了回看日：改回未结。只写状态与时间，不碰内容列。"""
    await session.execute(
        sql_update(AssistantFollowup)
        .where(
            AssistantFollowup.bot_id == bot_id,
            AssistantFollowup.status == "snoozed",
            AssistantFollowup.review_on <= today,
        )
        .values(status="open", updated_at=utcnow())
    )


async def _rows(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *where: ColumnElement[bool],
    limit: int | None = None,
    newest: bool = False,
) -> list[AssistantFollowup]:
    # 重新从库里取：会话里可能留着这些行更早的样子（别的事务改过状态）。
    stmt = (
        select(AssistantFollowup)
        .where(AssistantFollowup.bot_id == bot_id, *where)
        .execution_options(populate_existing=True)
    )
    stmt = stmt.order_by(AssistantFollowup.seq.desc() if newest else AssistantFollowup.seq)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = list(await session.scalars(stmt))
    # session.scalars 不自动还原。
    await sealed.open_rows(session, rows)
    return rows


def _due(today: date) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (
        AssistantFollowup.status == "open",
        or_(AssistantFollowup.review_on <= today, AssistantFollowup.due_on <= today),
    )


async def due(session: AsyncSession, bot_id: uuid.UUID, today: date) -> list[AssistantFollowup]:
    """今天该看的：未结且回看日或期限已到。幂等，同一天反复调用结果相同。

    Args:
        today: 所有者时区的今天
    """
    await _wake(session, bot_id, today)
    return await _rows(session, bot_id, *_due(today))


async def list_(
    session: AsyncSession, bot_id: uuid.UUID, scope: str, *, today: date, limit: int = 50
) -> list[AssistantFollowup]:
    """按范围列出：today（同 `due`）、open（全部未结，含推后的）、all（含已关闭，新的在前）、
    或一个分类名（该分类的未结）。

    Raises:
        FollowupError: invalid_scope
    """
    if scope not in SCOPES:
        raise FollowupError("invalid_scope")
    await _wake(session, bot_id, today)
    if scope == "today":
        return await _rows(session, bot_id, *_due(today), limit=limit)
    if scope == "all":
        return await _rows(session, bot_id, limit=limit, newest=True)
    where: list[ColumnElement[bool]] = [AssistantFollowup.status.in_(LIVE)]
    if scope != "open":
        where.append(AssistantFollowup.category == scope)
    return await _rows(session, bot_id, *where, limit=limit)


async def close_for_message(
    session: AsyncSession, bot_id: uuid.UUID, message_id: str
) -> list[AssistantFollowup]:
    """起因消息被撤回：这位助理由它引起、还未结的条目自动关闭。返回被关闭的行。"""
    if not message_id:
        return []
    rows = await _rows(
        session,
        bot_id,
        AssistantFollowup.status.in_(LIVE),
        AssistantFollowup.origin_message_id == message_id,
    )
    now = utcnow()
    for row in rows:
        row.status, row.close_code = "dropped", RECALLED
        row.closed_at = row.updated_at = now
    await session.flush()
    await _retire(session, bot_id, [row.seq for row in rows if row.needs_owner])
    return rows


def _owner_waiting(bot_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    return (
        AssistantFollowup.bot_id == bot_id,
        AssistantFollowup.status == "open",
        AssistantFollowup.needs_owner.is_(True),
    )


async def needs_owner(
    session: AsyncSession, bot_id: uuid.UUID, today: date, *, limit: int
) -> list[AssistantFollowup]:
    """要所有者当面处理、还未结的条目（推后的到了回看日先改回未结），编号小的在前，内容已还原。"""
    await _wake(session, bot_id, today)
    return await _rows(session, bot_id, *_owner_waiting(bot_id)[1:], limit=limit)


async def needs_owner_count(session: AsyncSession, bot_id: uuid.UUID) -> int:
    found = await session.scalar(
        select(func.count()).select_from(AssistantFollowup).where(*_owner_waiting(bot_id))
    )
    return int(found or 0)
