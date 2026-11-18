"""个人助理的事项总表：每条未结的待跟进推导出阶段与停滞，三处展示（页面、「看板」、摘要）共用。

阶段只在读取时算，不落库，免得与分类、子任务状态两份状态互相打架。优先级从上到下：
卡在权限（挂着未解决的缺口）→ 等你（needs_owner 或 decide）→ 等别人（waiting 或同事在办）→
排期（回看日在今天之后）→ 在推进。
停滞只对「在推进」「等别人」判断：在推进的 2 个工作日没有进展；等别人的过了期限，或 5 个工作日
没有进展。进展时间取待跟进的 `progress_at` 与挂着的子任务最后一次变化中较晚的那个；推后过、已到
回看日的从回看日起算（推后的那几天不算没动）；子任务还在跑就不算停滞。

在途审批单（写操作）单独列在「等你」里，第一期不挂到待跟进上。只在平台自己的代码里以元数据挑行，
内容由 `followups.list_` 还原。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, gaps, jobs, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import (
    AssistantApproval,
    AssistantFollowup,
    AssistantGap,
    AssistantJob,
    AssistantSettings,
)
from coreman.core.timeutils import aware_utc

GROUPS = ("owner", "stalled", "working", "others", "gap", "scheduled")
STALL_WORKING = 2
STALL_WAITING = 5
# 卡片上「排期」只列这么多天以内的；页面列全部。
SCHEDULE_DAYS = 7
MAX_SUGGESTION = 100
# 摘要与看板里在途审批单最多列这么多张，多的只说个数。
APPROVALS_MAX = 10
_SUGGESTION = "建议："


def workdays_between(start: date, end: date, workdays: Iterable[int]) -> int:
    """`start` 之后到 `end`（含）之间有几个工作日；`end` 不晚于 `start` 时为 0。"""
    days = set(workdays)
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        count += day.isoweekday() in days
        day += timedelta(days=1)
    return count


def stage(row: AssistantFollowup, today: date, blocked: frozenset[uuid.UUID] = frozenset()) -> str:
    if blocked and row.gap_id in blocked:
        return "gap"
    if row.needs_owner or row.category == "decide":
        return "owner"
    if row.category == "waiting" or row.handler == "person":
        return "others"
    if row.review_on is not None and row.review_on > today:
        return "scheduled"
    return "working"


def is_stalled(
    stage_: str, row: AssistantFollowup, job: AssistantJob | None, idle: int, today: date
) -> bool:
    if stage_ == "others":
        return (row.due_on is not None and row.due_on < today) or idle >= STALL_WAITING
    if stage_ == "working":
        if job is not None and job.status in jobs.LIVE:
            return False
        return idle >= STALL_WORKING
    return False


def idle_from(last_day: date, row: AssistantFollowup, today: date) -> date:
    """从哪天起算没动：最后一次进展那天；到了回看日的取回看日与它较晚的，推后的日子不算没动。"""
    if row.review_on is not None and row.review_on <= today:
        return max(last_day, row.review_on)
    return last_day


def suggestion(row: AssistantFollowup) -> str | None:
    """备注里第一行以「建议：」开头的内容（助理给所有者拍板用的建议）。"""
    for line in (row.notes_enc or "").split("\n"):
        line = line.strip()
        if line.startswith(_SUGGESTION):
            text = line[len(_SUGGESTION) :].strip()
            return text[:MAX_SUGGESTION] or None
    return None


@dataclass(frozen=True)
class Item:
    row: AssistantFollowup
    stage: str
    stalled: bool
    idle_days: int
    job: AssistantJob | None
    gap: AssistantGap | None = None


@dataclass(frozen=True)
class Gap:
    """一个未解决的缺口：怎么开，卡住了哪几条（编号）。"""

    row: AssistantGap
    fix: str
    followups: list[int]


@dataclass(frozen=True)
class Board:
    today: date
    groups: dict[str, list[Item]]
    approvals: list[AssistantApproval]
    approvals_total: int
    approval_items: dict[uuid.UUID, int]
    approvals_newest: datetime | None
    gaps: list[Gap] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(items) for name, items in self.groups.items()}


async def pending_approvals(
    session: AsyncSession, bot_id: uuid.UUID
) -> tuple[list[AssistantApproval], int, datetime | None, dict[uuid.UUID, int]]:
    """在途的审批单：每张单一行（早的在前，至多 `APPROVALS_MAX` 张，内容已还原）、总张数、最晚
    一张的提交时刻，以及合单的项数（按代表那张单的行的编号）。一轮里合成一张的几项只算一张。"""
    pending = (AssistantApproval.bot_id == bot_id, AssistantApproval.status == "pending")
    key = func.coalesce(AssistantApproval.batch_id, AssistantApproval.id)
    total, newest = (
        await session.execute(
            select(func.count(func.distinct(key)), func.max(AssistantApproval.created_at)).where(
                *pending
            )
        )
    ).one()
    grouped = (
        await session.execute(
            select(key, func.count())
            .where(*pending)
            .group_by(key)
            .order_by(func.min(AssistantApproval.created_at), key)
            .limit(APPROVALS_MAX)
        )
    ).all()
    counts = {group: int(count) for group, count in grouped}
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .where(*pending, key.in_(list(counts)))
            .order_by(AssistantApproval.created_at, AssistantApproval.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    first: dict[uuid.UUID, AssistantApproval] = {}
    for row in rows:
        first.setdefault(row.batch_id or row.id, row)
    shown = [first[group] for group in counts if group in first]
    sizes = {first[group].id: counts[group] for group in counts if group in first}
    return shown, int(total or 0), newest, sizes


def _last(row: AssistantFollowup, job: AssistantJob | None) -> datetime:
    last = aware_utc(row.progress_at)
    if job is not None and job.updated_at is not None:
        last = max(last, aware_utc(job.updated_at))
    return last


async def build(
    session: AsyncSession, bot_id: uuid.UUID, prefs: AssistantSettings, now: datetime
) -> Board:
    """这位助理此刻的总表。会把到了回看日的推后条目改回未结（同 `followups.list_`）。"""
    today = assistant_settings.local_today(prefs, now)
    rows = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
    live_gaps = await gaps.listing(session, bot_id, gaps.LIVE)
    by_id = {row.id: row for row, _ in live_gaps}
    blocked = frozenset(by_id)
    job_ids = {row.job_id for row in rows if row.job_id is not None}
    linked: dict[uuid.UUID, AssistantJob] = {}
    if job_ids:
        found = list(
            await session.scalars(
                select(AssistantJob)
                .where(AssistantJob.bot_id == bot_id, AssistantJob.id.in_(job_ids))
                .execution_options(populate_existing=True)
            )
        )
        await sealed.open_rows(session, found)
        linked = {job.id: job for job in found}
    groups: dict[str, list[Item]] = {name: [] for name in GROUPS}
    for row in rows:
        job = linked.get(row.job_id) if row.job_id else None
        stage_ = stage(row, today, blocked)
        last_day = assistant_settings.local_today(prefs, _last(row, job))
        idle = workdays_between(idle_from(last_day, row, today), today, prefs.workdays)
        stalled = is_stalled(stage_, row, job, idle, today)
        gap = by_id.get(row.gap_id) if row.gap_id else None
        groups["stalled" if stalled else stage_].append(Item(row, stage_, stalled, idle, job, gap))
    far = date.max
    groups["owner"].sort(key=lambda i: (i.row.due_on or far, i.row.seq))
    groups["stalled"].sort(key=lambda i: (-i.idle_days, i.row.seq))
    groups["scheduled"].sort(key=lambda i: (i.row.review_on or far, i.row.seq))
    approvals, total, newest, sizes = await pending_approvals(session, bot_id)
    entries = [Gap(row, await gaps.fix_for(session, bot_id, row), seqs) for row, seqs in live_gaps]
    return Board(today, groups, approvals, total, sizes, newest, entries)
