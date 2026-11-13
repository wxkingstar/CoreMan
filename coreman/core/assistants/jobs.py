"""个人助理的子任务：主会话派出去的一件事，在自己的会话里跑，结论回到主会话。

一行 `assistant_jobs` 是一个子任务，可以跑多轮：首轮的文本是交代，之后每次追问一轮。每一轮是
一个 `assistant_subtask` 任务，会话键 `<主会话键>#job:<编号>`，只经 `platform_turns.enqueue`
入队，载荷里只有编号。标题、交代、追问、结论、卡住的原因经 ORM 读写，由 `sealed` 的 flush 钩子
封存、会话取出时还原；状态、轮数、任务号、时间是明文。

所有函数按 `bot_id` 过滤：一位助理看不到也动不了别人的子任务。只 flush，不提交。经 ORM 写入
不会签发数据密钥，取不到密钥时内容列只会写成空，所以要写内容的地方先确认密钥可用，不可用就报
`content_locked`。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, platform_turns, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantJob, AssistantSettings, Bot
from coreman.core.timeutils import aware_utc, utcnow

# 同一位助理同时进行中的子任务、助理自己每天能派的个数、一个子任务最多几轮（首轮加追问）。
MAX_ACTIVE = 3
MAX_DAILY = 20
MAX_ROUNDS = 6
# 每次派出或追问起这么久还没结束，记失败。
LIFETIME = timedelta(hours=2)
MAX_TITLE = 200
MAX_BRIEF = 8000
MAX_MESSAGE = 4000
MAX_RESULT = 20000
MAX_REASON = 1000
LIVE = ("queued", "running")
ENDED = ("done", "failed", "cancelled", "blocked")
# `list_` 的范围：进行中的、卡住等人决定的、全部（新的在前）。
SCOPES: dict[str, tuple[str, ...] | None] = {
    "active": LIVE,
    "blocked": ("blocked",),
    "recent": None,
}


class JobError(Exception):
    """调用方能照着改的拒绝；`code` 原样作为工具的 error 返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def session_key_for(chat_id: str, job_id: uuid.UUID) -> str:
    """子任务自己的会话键：主会话键加编号。"""
    return f"{chat_id}{platform_turns.JOB_SEPARATOR}{job_id}"


def job_id_of(session_key: str | None) -> uuid.UUID | None:
    """子任务会话键里的编号；不是子任务会话返回 None。"""
    _, found, tail = (session_key or "").partition(platform_turns.JOB_SEPARATOR)
    if not found:
        return None
    try:
        return uuid.UUID(tail)
    except ValueError:
        return None


def parse_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise JobError("job_not_found") from None


def view(row: AssistantJob, *, full: bool = False) -> dict[str, Any]:
    """给助理（以及之后的页面）看的一条；`full` 时带交代、结论与卡住的原因。"""
    out: dict[str, Any] = {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "title": row.title_enc,
        "rounds": row.rounds,
        "created_at": row.created_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "has_result": bool(row.result_enc),
    }
    if row.error_code:
        out["error_code"] = row.error_code
    if full:
        out["brief"] = row.brief_enc
        out["result"] = row.result_enc
        out["blocked_reason"] = row.blocked_enc
    if sealed.is_locked(row):
        out["locked"] = True
    return out


def _clean(value: str, limit: int, code: str) -> str:
    out = value.strip()
    if not out or len(out) > limit:
        raise JobError(code)
    return out


async def _admit(
    session: AsyncSession,
    bot: Bot,
    prefs: AssistantSettings,
    *,
    new: bool,
    budget: bool,
    now: datetime,
) -> None:
    """再占一个「进行中」的名额之前的检查；之后本事务独占这位助理的名额计数。

    Args:
        new: 新派一个（数当天个数）；追问不算新的
        budget: 要不要看每日轮次预算（无人在场的轮次与平台派的要看）

    Raises:
        JobError: content_locked / too_many_jobs / daily_job_limit / daily_budget_exhausted
    """
    if not await sealed.key_ready(session, bot.id):
        raise JobError("content_locked")
    # 不锁 bots 的行（那一行别处一直在更新）；并发的两次派出在这里排队。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assistant-jobs:{bot.id}"},
    )
    live = await session.scalar(
        select(func.count())
        .select_from(AssistantJob)
        .where(AssistantJob.bot_id == bot.id, AssistantJob.status.in_(LIVE))
    )
    if (live or 0) >= MAX_ACTIVE:
        raise JobError("too_many_jobs")
    today = assistant_settings.local_today(prefs, now)
    if new:
        start, end = delivery.day_bounds(delivery.zone_named(prefs.timezone), today)
        made = await session.scalar(
            select(func.count())
            .select_from(AssistantJob)
            .where(
                AssistantJob.bot_id == bot.id,
                AssistantJob.origin == "assistant",
                AssistantJob.created_at >= start,
                AssistantJob.created_at < end,
            )
        )
        if (made or 0) >= MAX_DAILY:
            raise JobError("daily_job_limit")
    if budget and await delivery.budget_left(session, bot, today) <= 0:
        raise JobError("daily_budget_exhausted")


async def _enqueue_round(session: AsyncSession, bot: Bot, row: AssistantJob) -> None:
    """给这个子任务排下一轮：载荷只有编号与轮次，文本开轮时从行里取。

    Raises:
        JobError: assistant_unavailable（这位助理现在跑不了平台来源的轮次）
    """
    row.rounds += 1
    task = await platform_turns.enqueue(
        session,
        bot,
        SUBTASK_KIND,
        {"job_id": str(row.id), "round": row.rounds},
        session_key=row.session_key,
        dedupe_key=f"assistant-job:{row.id}:round:{row.rounds}",
    )
    if task is None:
        raise JobError("assistant_unavailable")
    row.last_task_id = task.id


async def spawn(
    session: AsyncSession,
    bot: Bot,
    *,
    title: str,
    brief: str,
    origin: str = "assistant",
    report: bool = True,
    attended: bool = True,
    now: datetime | None = None,
) -> AssistantJob:
    """派一个子任务并排上它的第一轮。

    Args:
        origin: `assistant`（助理用工具派的）或 `system`（平台派的，不占每天的个数）
        report: 结束后要不要把结论作为一轮投回主会话
        attended: 派出它的那一轮所有者在不在场；不在场的要看每日轮次预算

    Raises:
        JobError: invalid_title / invalid_brief / assistant_unavailable / content_locked /
            too_many_jobs / daily_job_limit / daily_budget_exhausted
    """
    now = aware_utc(now or utcnow())
    title = _clean(title, MAX_TITLE, "invalid_title")
    brief = _clean(brief, MAX_BRIEF, "invalid_brief")
    prefs = await assistant_settings.get(session, bot.id)
    if not assistant_settings.identified(prefs):
        raise JobError("assistant_unavailable")
    await _admit(
        session,
        bot,
        prefs,
        new=origin == "assistant",
        budget=not attended or origin == "system",
        now=now,
    )
    job_id = uuid.uuid4()
    row = AssistantJob(
        id=job_id,
        bot_id=bot.id,
        kind="subtask",
        origin=origin,
        status="queued",
        title_enc=title,
        brief_enc=brief,
        session_key=session_key_for(prefs.chat_id, job_id),
        origin_session_key=prefs.chat_id,
        rounds=0,
        report=report,
        report_seq=0,
        report_failures=0,
        deadline_at=now + LIFETIME,
        created_at=now,
        updated_at=now,
    )
    # 先入队再建行：入不了队就什么都不留下。
    await _enqueue_round(session, bot, row)
    session.add(row)
    await session.flush()
    return row


async def get(
    session: AsyncSession, bot_id: uuid.UUID, job_id: uuid.UUID, *, lock: bool = False
) -> AssistantJob:
    """这位助理的这个子任务，内容已还原。

    Raises:
        JobError: job_not_found（包括编号是别的助理的）
    """
    stmt = select(AssistantJob).where(AssistantJob.bot_id == bot_id, AssistantJob.id == job_id)
    if lock:
        stmt = stmt.with_for_update()
    # session.scalar 取单个实体：SealedSession 自动还原内容列。
    row: AssistantJob | None = await session.scalar(stmt)
    if row is None:
        raise JobError("job_not_found")
    return row


async def list_(
    session: AsyncSession, bot_id: uuid.UUID, *, scope: str = "active", limit: int = 20
) -> list[AssistantJob]:
    """这位助理的子任务，新的在前，内容已还原。

    Raises:
        JobError: invalid_scope
    """
    if scope not in SCOPES:
        raise JobError("invalid_scope")
    stmt = select(AssistantJob).where(AssistantJob.bot_id == bot_id)
    statuses = SCOPES[scope]
    if statuses is not None:
        stmt = stmt.where(AssistantJob.status.in_(statuses))
    rows = list(
        await session.scalars(
            stmt.order_by(AssistantJob.created_at.desc(), AssistantJob.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows


async def message(
    session: AsyncSession,
    bot: Bot,
    job_id: uuid.UUID,
    text_: str,
    *,
    attended: bool = True,
    now: datetime | None = None,
) -> AssistantJob:
    """向一个已经有结论（或卡住）的子任务追问：同一个会话里再跑一轮。

    上一轮的结论留着，直到新的一轮写下新的；还没投到主会话的那一次投回作废。

    Raises:
        JobError: invalid_text / job_not_found / job_busy / job_closed / job_round_limit /
            content_locked / too_many_jobs / daily_budget_exhausted / assistant_unavailable
    """
    now = aware_utc(now or utcnow())
    body = _clean(text_, MAX_MESSAGE, "invalid_text")
    prefs = await assistant_settings.get(session, bot.id)
    # 先确认是自己的子任务，再占名额：别人的编号不该先撞上「名额满了」。
    await get(session, bot.id, job_id)
    await _admit(session, bot, prefs, new=False, budget=not attended, now=now)
    row = await get(session, bot.id, job_id, lock=True)
    if row.kind != "subtask":
        raise JobError("job_not_found")
    if row.status in LIVE:
        raise JobError("job_busy")
    if row.status not in ("done", "blocked"):
        raise JobError("job_closed")
    if sealed.is_locked(row):
        raise JobError("content_locked")
    if row.rounds >= MAX_ROUNDS:
        raise JobError("job_round_limit")
    if row.result_task_id is not None and row.reported_at is None:
        await tasks.request_cancel(session, row.result_task_id, "job_reopened")
    row.message_enc, row.blocked_enc = body, None
    row.status, row.error_code, row.finished_at = "queued", None, None
    row.result_task_id, row.reported_at, row.report_after = None, None, None
    row.report_failures, row.report_code = 0, None
    row.deadline_at, row.updated_at = now + LIFETIME, now
    await _enqueue_round(session, bot, row)
    await session.flush()
    return row
