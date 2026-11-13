"""「我的助理」页的接口：概览、等你出手、子任务、最近落地、设置、停用。

只对所有者本人：要求交互登录会话（机器人令牌 403），助理由登录人推出。读取密文的接口在返回前
记一次访问（`access_log.record`）。待跟进与知识页在 `me_assistant_followups.py`、
`me_assistant_notes.py`。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, followups, jobs, owner_view, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import User
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)

# 「最近结束」看最近这么多个子任务里已经结束的。
JOB_LIMIT = 20
JOB_ERRORS: dict[str, tuple[int, str]] = {
    "job_not_found": (404, "这个子任务不存在"),
    "job_closed": (409, "这个子任务已经结束"),
}


@router.get("")
async def overview(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """概览；没有助理时回空状态与开通指引（不是 404：页面要据此显示怎么开通）。"""
    bot = await owner_view.current(session, actor.id)
    data = (
        await owner_view.absent(session, actor)
        if bot is None
        else await owner_view.overview(session, bot, utcnow())
    )
    # 概览会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/jobs")
async def job_list(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """正在跑与最近结束的子任务与调度，含交代原文、轮数与结论。

    调度公共员工与子任务同表：`kind` 是 `bot_help` 的带目标员工的名字（`target`）；`origin` 是
    `system` 的是平台派的（下班整理），页面上与所有者让助理派的分开显示。
    """
    active = await jobs.list_(session, own.bot.id, scope="active", limit=JOB_LIMIT)
    recent = [
        row
        for row in await jobs.list_(session, own.bot.id, scope="recent", limit=JOB_LIMIT)
        if row.status in jobs.ENDED
    ]
    # 核心函数已经还原过；这里按查看者再核一次，不是所有者本人就全部收回。
    await sealed.open_rows(session, [*active, *recent], viewer=own.actor.id)
    names = await owner_view.target_names(session, [*active, *recent])
    await access_log.record(session, own.bot.id, own.actor.id, "jobs.read", "list")
    return {
        "code": 0,
        "data": {
            "active": [owner_view.job(row, names) for row in active],
            "recent": [owner_view.job(row, names) for row in recent],
        },
    }


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(
    job_id: uuid.UUID,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者取消一个进行中的子任务或调度；`report=True` 让助理在主会话里得知这件事。"""
    try:
        row = await jobs.cancel(session, own.bot.id, job_id, code="cancelled_by_owner", report=True)
    except jobs.JobError as exc:
        await session.rollback()
        raise refuse(exc.code, JOB_ERRORS) from None
    data = {"id": str(row.id), "status": row.status}
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/attention")
async def attention(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """等你出手：待批的审批单、被卡住的子任务、今天到期的待跟进。"""
    bot, actor = own.bot, own.actor
    prefs = await assistant_settings.get(session, bot.id)
    today = assistant_settings.local_today(prefs, utcnow())
    link = owner_view.approval_link()
    pending = await owner_view.approvals(session, bot, actor.id, owner_view.PENDING)
    blocked = await jobs.list_(session, bot.id, scope="blocked", limit=JOB_LIMIT)
    due = await followups.due(session, bot.id, today)
    # 核心函数已经还原过；这里按查看者再核一次。
    await sealed.open_rows(session, [*blocked, *due], viewer=actor.id)
    names = await owner_view.target_names(session, blocked)
    await access_log.record(session, bot.id, actor.id, "approvals.read", "pending")
    await access_log.record(session, bot.id, actor.id, "jobs.read", "blocked")
    await access_log.record(session, bot.id, actor.id, "followups.read", "today")
    data = {
        "today": today.isoformat(),
        "approvals": [owner_view.pending_view(row, link) for row in pending],
        "jobs": [owner_view.job(row, names) for row in blocked],
        "followups": [followups.view(row) for row in due],
    }
    # 取今天到期的条目会把到了回看日的推后条目改回未结。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/approvals/recent")
async def recent_approvals(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """最近落地：全部终态的审批单（已执行、被拒、撤回、失败、过期、作废），给本人解密后的摘要与理由。"""
    rows = await owner_view.approvals(session, own.bot, own.actor.id, owner_view.SETTLED)
    await access_log.record(session, own.bot.id, own.actor.id, "approvals.read", "recent")
    return {"code": 0, "data": {"items": [owner_view.settled_view(row) for row in rows]}}
