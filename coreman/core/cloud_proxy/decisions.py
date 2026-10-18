"""把 Tower 的决定落到本地挂起单：回调与对账共用这一份状态迁移，规则只能有一处。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.cloud_proxy import resume
from coreman.core.db.models.cloud_proxy import CloudPendingApproval
from coreman.core.timeutils import utcnow

# Tower 的审批编号会进 Agent 提示，只收它实际使用的字符
APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class Applied:
    """known=False：不是本代理发起的审批（或编号对不上）；resumed：排上了续接任务。"""

    known: bool
    resumed: bool = False


async def apply(
    session: AsyncSession,
    *,
    approval_id: str,
    external_id: str,
    status: str,
    reason: str | None,
    decided_at: datetime | None,
) -> Applied:
    """调用方须先校验 status 属于 RESUME_STATUSES、approval_id 合乎 APPROVAL_ID，并负责提交。"""
    row = await session.scalar(
        select(CloudPendingApproval)
        .where(CloudPendingApproval.external_id == external_id)
        .with_for_update()
    )
    if row is None or (row.approval_id is not None and row.approval_id != approval_id):
        # 别的发送端流程或非代理发起的审批：挂起单不归它管
        return Applied(known=False)
    if row.approval_id is None:
        row.approval_id = approval_id
    if row.decided_at is None:
        row.decided_at = decided_at or utcnow()
    # 只从 pending 往前走：Agent 先自己取令牌执行（executed）或已有决定的单不回退、不再续接
    if row.status != "pending":
        return Applied(known=True)
    row.status = status
    row.reason = resume.clean_reason(reason) if reason else None
    task = await resume.start(session, row)
    return Applied(known=True, resumed=task is not None)
