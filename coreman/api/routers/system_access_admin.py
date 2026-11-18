"""业务系统权限申请的管理台：平台管理员（能改系统白名单的人）列出并批准或拒绝。

批准 = 把这位助理加进系统白名单 + 与管理台改系统权限同一段写入（`apply_grants`），
再由 `settle_approved` 收尾，全在一个事务里提交。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.permissions import require_roles
from coreman.api.routers.infrastructure_admin import audit, persist
from coreman.api.routers.me_assistant_common import refuse
from coreman.api.security import verify_csrf
from coreman.api.system_grants import apply_grants, current_grants, lock_bot
from coreman.core.assistants import access_requests
from coreman.core.db.models import Bot, BusinessSystem, SystemAccessRequest, User

MANAGERS = require_roles("ai_committee", "platform_admin")
router = APIRouter(
    prefix="/api/admin/system-access-requests",
    tags=["systems"],
    dependencies=[Depends(verify_csrf)],
)
PER_PAGE = 50
ERRORS = {
    "request_not_found": (404, "申请不存在"),
    "request_decided": (409, "这张申请已经处理过了"),
    "request_stale": (
        409,
        "这位助理已停用或这个缺口已不在等待申请，申请已自动撤回，没有开通",
    ),
    "system_not_requestable": (409, "系统已删除或停用"),
    "invalid_reason": (422, f"拒绝理由写一两句话，不超过 {access_requests.MAX_NOTE} 字"),
}


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=access_requests.MAX_NOTE * 4)


@router.get("")
async def listing(
    status: Literal["pending", "all"] = "pending",
    page: int = Query(1, ge=1),
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    where = [SystemAccessRequest.status == "pending"] if status == "pending" else []
    total = await session.scalar(
        select(func.count()).select_from(SystemAccessRequest).where(*where)
    )
    rows = (
        await session.execute(
            select(SystemAccessRequest, Bot.name, BusinessSystem.name, User.display_name)
            .join(Bot, Bot.id == SystemAccessRequest.bot_id)
            .join(BusinessSystem, BusinessSystem.key == SystemAccessRequest.system_key)
            .join(User, User.id == SystemAccessRequest.requested_by)
            .where(*where)
            .order_by(SystemAccessRequest.created_at.desc(), SystemAccessRequest.id)
            .offset((page - 1) * PER_PAGE)
            .limit(PER_PAGE)
        )
    ).all()
    items = [
        access_requests.view(row, bot_name=bot_name, system_name=system_name, requester=requester)
        for row, bot_name, system_name, requester in rows
    ]
    return {
        "code": 0,
        "data": {"items": items, "total": int(total or 0), "page": page, "per_page": PER_PAGE},
    }


async def _approve(
    session: AsyncSession, request: Request, user: User, request_id: uuid.UUID
) -> SystemAccessRequest:
    row, system, added = await access_requests.mark_approved(session, request_id, actor=user)
    bot = await session.get(Bot, row.bot_id)
    assert bot is not None  # mark_approved 刚核对过助理还在
    if added:
        # 与管理台改系统白名单同一口径：只在这一次确实把助理加进去时记。
        await audit(
            session,
            request,
            user,
            "system.update",
            "system",
            system.key,
            {"granted_bot_ids": [[], [str(bot.id)]]},
        )
    # 先锁机器人行再读现有授权：与管理台改权限、所有者一键开通互相排队。
    await lock_bot(session, bot)
    keys, writes = await current_grants(session, bot.id)
    await apply_grants(
        session,
        request,
        user,
        bot,
        keys=[*keys, row.system_key],
        write_keys=[*writes, row.system_key] if row.write else writes,
        comment=f"批准权限申请 {row.id}",
    )
    await access_requests.settle_approved(session, row)
    await audit(
        session,
        request,
        user,
        "system_access.approve",
        "bot",
        str(bot.id),
        {"system": row.system_key, "write": row.write},
    )
    return row


@router.post("/{request_id}/decide")
async def decide(
    request_id: uuid.UUID,
    body: DecideIn,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """批准或拒绝一张在途的申请。批准时发现申请已失效：撤回先提交，再把原因告诉管理员。"""
    try:
        if body.decision == "reject":
            row = await access_requests.reject(session, request_id, actor=user, note=body.note)
            await audit(
                session,
                request,
                user,
                "system_access.reject",
                "bot",
                str(row.bot_id),
                {"system": row.system_key},
            )
        else:
            row = await _approve(session, request, user, request_id)
    except access_requests.RequestError as exc:
        if exc.code == "request_stale":
            await session.commit()
        else:
            await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = {"id": str(row.id), "status": row.status}
    await persist(session, commit=True)
    return {"code": 0, "data": data}
