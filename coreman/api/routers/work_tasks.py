"""任务跟进：调度员分派的任务，列表与详情（第一期只读）。

所有登录的成员都能看，包括给 Agent 看的那一层（brief、记要、调度依据、事件细节）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.db.models import (
    WORK_TASK_STATUSES,
    Bot,
    GroupObserveDecision,
    User,
    WorkTask,
    WorkTaskEvent,
)

router = APIRouter(
    prefix="/api/admin/work-tasks", tags=["work-tasks"], dependencies=[Depends(verify_csrf)]
)


def _active(user: User) -> None:
    if user.status != "active":
        raise ApiError(403, 403, "账号未启用")


async def _names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Bot.id, Bot.name).where(Bot.id.in_(ids)))
    return {bot_id: name for bot_id, name in rows.all()}


def _bot(bot_id: uuid.UUID | None, names: dict[uuid.UUID, str]) -> dict[str, Any] | None:
    if bot_id is None:
        return None
    return {"id": str(bot_id), "name": names.get(bot_id, "已删除的员工")}


def task_out(row: WorkTask, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    """给人看的那一层。"""
    return {
        "code": row.code,
        "title": row.title,
        "summary": row.summary,
        "status": row.status,
        "platform": row.platform,
        "chat_id": row.chat_id,
        "requester_name": row.requester_name,
        "assignee": _bot(row.assignee_bot_id, names),
        "dispatcher": _bot(row.dispatcher_bot_id, names),
        "related_codes": list(row.related_codes or []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "closed_at": row.closed_at,
    }


@router.get("")
async def list_work_tasks(
    chat_id: str | None = None,
    status: str | None = None,
    assignee_bot_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    stmt = select(WorkTask)
    if chat_id:
        stmt = stmt.where(WorkTask.chat_id == chat_id)
    if status:
        if status not in WORK_TASK_STATUSES:
            raise ApiError(400, 400, "未知的任务状态")
        stmt = stmt.where(WorkTask.status == status)
    if assignee_bot_id:
        stmt = stmt.where(WorkTask.assignee_bot_id == assignee_bot_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            WorkTask.code.ilike(like) | WorkTask.title.ilike(like) | WorkTask.summary.ilike(like)
        )
    if before_id is not None:
        stmt = stmt.where(WorkTask.id < before_id)
    rows = list(await session.scalars(stmt.order_by(WorkTask.id.desc()).limit(limit)))
    names = await _names(
        session,
        {i for r in rows for i in (r.assignee_bot_id, r.dispatcher_bot_id) if i is not None},
    )
    grouped = await session.execute(select(WorkTask.status, func.count()).group_by(WorkTask.status))
    counts: dict[str, int] = {str(status): int(n) for status, n in grouped.all()}
    return {
        "code": 0,
        "data": {
            "items": [{**task_out(r, names), "id": r.id} for r in rows],
            "counts": counts,
        },
    }


@router.get("/{code}")
async def get_work_task(
    code: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _active(user)
    row = await session.scalar(select(WorkTask).where(WorkTask.code == code))
    if row is None:
        raise not_found("任务不存在")
    events = list(
        await session.scalars(
            select(WorkTaskEvent)
            .where(WorkTaskEvent.work_task_id == row.id)
            .order_by(WorkTaskEvent.id)
        )
    )
    decision = await session.get(GroupObserveDecision, row.decision_id) if row.decision_id else None
    names = await _names(
        session,
        {i for i in (row.assignee_bot_id, row.dispatcher_bot_id) if i is not None}
        | {uuid.UUID(e.actor_id) for e in events if e.actor_type == "bot" and e.actor_id},
    )
    return {
        "code": 0,
        "data": {
            **task_out(row, names),
            "events": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "actor_type": e.actor_type,
                    "actor_name": (
                        names.get(uuid.UUID(e.actor_id), "")
                        if e.actor_type == "bot" and e.actor_id
                        else ""
                    ),
                    "human_text": e.human_text,
                    "agent_detail": e.agent_detail,
                    "created_at": e.created_at,
                }
                for e in events
            ],
            # 给 Agent 看的那一层：页面上逐项展开。
            "agent": {
                "brief": row.brief,
                "digest": row.digest,
                "decision": (
                    {
                        "id": decision.id,
                        "probability": decision.probability,
                        "outcome": decision.outcome,
                        "recent_messages": (decision.state or {}).get("recent_messages") or [],
                        "dispatch": decision.dispatch,
                        "created_at": decision.created_at,
                    }
                    if decision is not None
                    else None
                ),
            },
        },
    }
