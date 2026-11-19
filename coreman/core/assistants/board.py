"""个人助理的事项总表：每条未结的待跟进推导出所处的状态，三处展示（页面、「看板」、摘要）共用。

五种状态，一条事项任何时候只处于其中一种（规格见
docs/superpowers/specs/2026-11-20-assistant-followup-states.md）：
- 等你（owner）：缺所有者的指示或信息——拍板、待批单、补信息、到日子的提醒、他自己在办。
- 执行中（working）：子任务在跑或排队，或助理在推进且最近有进展。
- 等别人（others）：已找了某个人，对方知道要提供什么；到了催办时间标「该催了」。
- 排期（scheduled）：现在做不了，有明确的启动日期（所有者改过期，或回看日在之后）。
- 停滞（stalled）：不在跑，也不知道怎么办或该找谁，也没有要所有者批的。

状态只在读取时算，不落库，免得与分类、子任务状态两份状态互相打架。判定顺序见 `classify`。
每条还带一个原因代码（`reason`），说明它为什么在这一组。

进展时间取待跟进的 `progress_at` 与挂着的子任务最后一次变化中较晚的那个；改过期、已到回看日的从
回看日起算（改期的那几天不算没动）。

在途审批单（写操作）单独列在「等你」里；挂在未结待跟进下的单归到那一条下面（该条目进「等你」），
不在单独的列表里重复。只在平台自己的代码里以元数据挑行，内容由 `sealed.open_rows` 还原。
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

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

GROUPS = ("owner", "working", "others", "scheduled", "stalled")
# 助理在推进的事这么多个工作日没有进展算停滞。
STALL_WORKING = 2
# 等别人：没有期限的这么多个工作日没有进展（催过的从上次催办起算）就该催了。
NUDGE_AFTER = 2
# 催满这么多次还没回，算停滞（没人回）。
MAX_NUDGES = 2
# 卡片上「排期」只列这么多天以内的；页面列全部。
SCHEDULE_DAYS = 7
MAX_SUGGESTION = 100
MAX_NEED = 100
# 摘要与看板里在途审批单最多列这么多张，多的只说个数。
APPROVALS_MAX = 10
_SUGGESTION = "建议："
_NEED = "需要你："
# 原因代码 → 给所有者看的一句话（卡片与摘要用；页面有自己的多语言文案）。
REASONS = {
    "gap": "要你开通权限",
    "approval": "有待批的单",
    "decide": "等你拍板",
    "info": "要你补信息",
    "remind": "到日子提醒你",
    "self": "你在办",
    "job_blocked": "子任务要你补信息",
    "job_running": "子任务在跑",
    "job_queued": "子任务排队中",
    "assistant": "助理在推进",
    "gap_requested": "已申请权限，等管理员",
    "waiting": "在等对方",
    "nudge_due": "该催了",
    "snoozed": "你改了期",
    "review": "到日子再启动",
    "gap_admin": "缺权限，要找管理员",
    "stuck_how": "不知道怎么做",
    "stuck_who": "不知道找谁",
    "stuck_failed": "反复失败",
    "no_reply": "催了还没回",
    "job_failed": "子任务失败",
    "idle": "几天没有进展",
}
# 所有者自己就能开（当面授权、填凭证、自己开放、确认申请）的缺口。
OWNER_FIXES = ("authorize", "credential", "self_grant", "request")


def workdays_between(start: date, end: date, workdays: Iterable[int]) -> int:
    """`start` 之后到 `end`（含）之间有几个工作日；`end` 不晚于 `start` 时为 0。"""
    days = set(workdays)
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        count += day.isoweekday() in days
        day += timedelta(days=1)
    return count


def gap_stage(row: AssistantGap, fix: str) -> str:
    """缺口卡住的事在哪一组：已申请在等管理员 → 等别人；所有者自己能开 → 等你；其余 → 停滞。"""
    if row.status == "requested":
        return "others"
    return "owner" if fix in OWNER_FIXES else "stalled"


def classify(
    row: AssistantFollowup,
    today: date,
    *,
    idle: int = 0,
    job: AssistantJob | None = None,
    gap: str | None = None,
    awaiting: bool = False,
) -> tuple[str, str]:
    """（状态, 原因代码）。

    Args:
        idle: 多少个工作日没有进展
        job: 挂着的子任务
        gap: 挂着未解决的缺口时它所在的组（`gap_stage`）
        awaiting: 挂着在途的审批单
    """
    if gap is not None:
        return gap, {"owner": "gap", "others": "gap_requested"}.get(gap, "gap_admin")
    if awaiting:
        return "owner", "approval"
    if row.status == "snoozed" and row.review_on is not None and row.review_on > today:
        return "scheduled", "snoozed"
    if row.category == "decide":
        return "owner", "decide"
    if row.needs_owner:
        return "owner", "info"
    if row.category == "remind" and (row.review_on is None or row.review_on <= today):
        return "owner", "remind"
    if row.handler == "owner":
        return "owner", "self"
    if row.stuck:
        return "stalled", "stuck_" + row.stuck
    if row.category == "waiting" or row.handler == "person":
        nudges = row.nudges or 0
        late = nudges == 0 and row.due_on is not None and row.due_on < today
        if late or idle >= NUDGE_AFTER:
            return ("stalled", "no_reply") if nudges >= MAX_NUDGES else ("others", "nudge_due")
        return "others", "waiting"
    if job is not None:
        if job.status in jobs.LIVE:
            return "working", "job_" + job.status
        if job.status == "blocked":
            return "owner", "job_blocked"
        if job.status == "failed":
            return "stalled", "job_failed"
    if row.review_on is not None and row.review_on > today:
        return "scheduled", "review"
    if idle >= STALL_WORKING:
        return "stalled", "idle"
    return "working", "assistant"


def idle_from(last_day: date, row: AssistantFollowup, today: date) -> date:
    """从哪天起算没动：最后一次进展那天；到了回看日的取回看日与它较晚的，改期的日子不算没动。"""
    if row.review_on is not None and row.review_on <= today:
        return max(last_day, row.review_on)
    return last_day


def _line_after(row: AssistantFollowup, prefix: str, limit: int) -> str | None:
    for line in (row.notes_enc or "").split("\n"):
        line = line.strip()
        if line.startswith(prefix):
            text = line[len(prefix) :].strip()
            return text[:limit] or None
    return None


def need(row: AssistantFollowup) -> str | None:
    """备注里第一行以「需要你：」开头的内容（助理要所有者补的信息）。"""
    return _line_after(row, _NEED, MAX_NEED)


def suggestion(row: AssistantFollowup) -> str | None:
    """备注里第一行以「建议：」开头的内容（助理给所有者拍板用的建议）。"""
    return _line_after(row, _SUGGESTION, MAX_SUGGESTION)


@dataclass(frozen=True)
class Item:
    row: AssistantFollowup
    stage: str
    reason: str
    idle_days: int
    job: AssistantJob | None
    gap: AssistantGap | None = None
    approvals: tuple[AssistantApproval, ...] = ()


@dataclass(frozen=True)
class Gap:
    """一个未解决的缺口：怎么开，卡住了哪几条（编号），列在哪一组（`gap_stage`）。"""

    row: AssistantGap
    fix: str
    followups: list[int]
    stage: str = "owner"


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
    session: AsyncSession, bot_id: uuid.UUID, *, exclude: Collection[uuid.UUID] = ()
) -> tuple[list[AssistantApproval], int, datetime | None, dict[uuid.UUID, int]]:
    """在途的审批单：每张单一行（早的在前，至多 `APPROVALS_MAX` 张，内容已还原）、总张数、最晚
    一张的提交时刻，以及合单的项数（按代表那张单的行的编号）。一轮里合成一张的几项只算一张。
    `exclude` 是已归到条目下的合单（`coalesce(batch_id, id)`）：不列、不计张数；最晚提交时刻仍按全部
    在途的单算，免得挂在老条目下的新单让摘要显得没有新东西。"""
    pending: tuple[Any, ...] = (
        AssistantApproval.bot_id == bot_id,
        AssistantApproval.status == "pending",
    )
    key = func.coalesce(AssistantApproval.batch_id, AssistantApproval.id)
    newest = await session.scalar(select(func.max(AssistantApproval.created_at)).where(*pending))
    if exclude:
        pending += (key.not_in(list(exclude)),)
    total = await session.scalar(select(func.count(func.distinct(key))).where(*pending))
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


async def linked_approvals(
    session: AsyncSession,
    bot_id: uuid.UUID,
    followup_ids: Collection[uuid.UUID],
    skip: Collection[uuid.UUID] = (),
) -> tuple[dict[uuid.UUID, list[AssistantApproval]], dict[uuid.UUID, int], set[uuid.UUID]]:
    """挂在这些条目下的在途审批单：按条目分组的代表行（早的在前，内容已还原）、各合单的项数、
    已归到条目下的合单键。一轮里合成的一张单按代表行（最早的一行）算：整张单要么归到它挂的条目
    下，要么留在单独的列表里。挂在 `skip` 里的条目（不会出现在「等你」）下的单不归。"""
    if not followup_ids:
        return {}, {}, set()
    key = func.coalesce(AssistantApproval.batch_id, AssistantApproval.id)
    base = (AssistantApproval.bot_id == bot_id, AssistantApproval.status == "pending")
    touched = select(key).where(*base, AssistantApproval.followup_id.in_(list(followup_ids)))
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .where(*base, key.in_(touched))
            .order_by(AssistantApproval.created_at, AssistantApproval.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    first: dict[uuid.UUID, AssistantApproval] = {}
    sizes: dict[uuid.UUID, int] = {}
    for row in rows:
        group = row.batch_id or row.id
        first.setdefault(group, row)
        sizes[group] = sizes.get(group, 0) + 1
    ids, skipped = set(followup_ids), set(skip)
    held: dict[uuid.UUID, list[AssistantApproval]] = {}
    items: dict[uuid.UUID, int] = {}
    claimed: set[uuid.UUID] = set()
    for group, rep in first.items():
        target = rep.followup_id
        if target is None or target not in ids or target in skipped:
            continue
        held.setdefault(target, []).append(rep)
        items[rep.id] = sizes[group]
        claimed.add(group)
    return held, items, claimed


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
    entries = []
    for gap_row, seqs in live_gaps:
        fix = await gaps.fix_for(session, bot_id, gap_row)
        entries.append(Gap(gap_row, fix, seqs, gap_stage(gap_row, fix)))
    gap_by_id = {entry.row.id: entry for entry in entries}
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
    stuck = [row.id for row in rows if row.gap_id in gap_by_id]
    held, held_items, claimed = await linked_approvals(
        session, bot_id, [row.id for row in rows], stuck
    )
    groups: dict[str, list[Item]] = {name: [] for name in GROUPS}
    for row in rows:
        job = linked.get(row.job_id) if row.job_id else None
        last_day = assistant_settings.local_today(prefs, _last(row, job))
        idle = workdays_between(idle_from(last_day, row, today), today, prefs.workdays)
        entry = gap_by_id.get(row.gap_id) if row.gap_id else None
        stage_, reason = classify(
            row,
            today,
            idle=idle,
            job=job,
            gap=entry.stage if entry else None,
            awaiting=row.id in held,
        )
        gap = entry.row if entry else None
        groups[stage_].append(
            Item(row, stage_, reason, idle, job, gap, tuple(held.get(row.id, ())))
        )
    far = date.max
    groups["owner"].sort(key=lambda i: (i.row.due_on or far, i.row.seq))
    groups["others"].sort(key=lambda i: (i.reason != "nudge_due", i.row.due_on or far, i.row.seq))
    groups["stalled"].sort(key=lambda i: (-i.idle_days, i.row.seq))
    groups["scheduled"].sort(key=lambda i: (i.row.review_on or far, i.row.seq))
    approvals, total, newest, sizes = await pending_approvals(session, bot_id, exclude=claimed)
    return Board(today, groups, approvals, total, {**sizes, **held_items}, newest, entries)
