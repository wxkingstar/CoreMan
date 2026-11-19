"""「从现在开始」：所有者正式启用助理前，把试用期间攒下的事项一次清掉，从干净的状态开始。

清掉的：未结（含推后）的待跟进记为不再需要；进行中的子任务与调度取消，已经结束、还没投回主会话
的结论不再投回；待批的审批单撤回（审批系统里同步撤）；还没交回助理的收件箱条目作废；还没发出的
摘要语句丢掉。

留着的：主会话与子任务的历史对话、转录、知识页（含口吻页）、设置、能力缺口（缺什么权限是事实，
清了下次还会再报）、已经落地的审批流水。清完给助理入一条提醒：之前的事项已经清掉，历史只作背景。

一个事务里做完，调用方提交。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, delivery_checks, digest, followups, jobs
from coreman.core.assistants.watch_inbox import void_all
from coreman.core.db.models import AssistantFollowup, AssistantJob, AssistantReport
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 取消、撤回、作废时记下的原因代码。
CODE = "owner_reset"
REMINDER = "owner_reset"


@dataclass(frozen=True)
class Cleared:
    followups: int
    jobs: int
    approvals: int
    inbox: int
    reports: int

    def payload(self) -> dict[str, int]:
        return asdict(self)


async def _drop_followups(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> int:
    await followups._lock(session, bot_id)
    rows = list(
        await session.scalars(
            select(AssistantFollowup)
            .where(
                AssistantFollowup.bot_id == bot_id,
                AssistantFollowup.status.in_(followups.LIVE),
            )
            .order_by(AssistantFollowup.seq)
            .with_for_update()
        )
    )
    for row in rows:
        row.status = "dropped"
        row.closed_at = row.updated_at = now
    await session.flush()
    # 摘要、看板卡片上这些条目的「现在处理」按钮置灰。
    await digest.retire(session, bot_id, [row.seq for row in rows])
    return len(rows)


async def _skip_unreported(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> None:
    """已经结束、还没开始投回的结论不再投回（正在投回的那一轮不动）。"""
    await session.execute(
        update(AssistantJob)
        .where(
            AssistantJob.bot_id == bot_id,
            AssistantJob.report.is_(True),
            AssistantJob.reported_at.is_(None),
            AssistantJob.result_task_id.is_(None),
            AssistantJob.status.in_(jobs.ENDED),
        )
        .values(reported_at=now, report_code="not_needed", updated_at=now)
    )


async def start_fresh(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> Cleared:
    """清掉这位助理积压的事项，并告诉助理从此刻起重新开始。返回各清了多少。"""
    dropped = await _drop_followups(session, bot_id, now)
    cancelled = await jobs.cancel_all(session, bot_id, CODE, now=now)
    await _skip_unreported(session, bot_id, now)
    withdrawn = await approvals.withdraw_pending(session, bot_id, CODE)
    voided = await void_all(session, bot_id, CODE)
    reports = await session.execute(
        delete(AssistantReport).where(
            AssistantReport.bot_id == bot_id, AssistantReport.sent_at.is_(None)
        )
    )
    cleared = Cleared(
        followups=dropped,
        jobs=cancelled,
        approvals=withdrawn,
        inbox=voided,
        reports=int(getattr(reports, "rowcount", 0) or 0),
    )
    # 先作废再入：这条提醒本身不能被上面的作废带走。
    await delivery_checks.add_reminder(
        session,
        bot_id,
        f"{REMINDER}:{int(now.timestamp())}",
        level="immediate",
        now=now,
        summary={"reminder": REMINDER, "at": now.isoformat(), **cleared.payload()},
    )
    log.info("assistant_reset", bot_id=str(bot_id), **cleared.payload())
    return cleared
