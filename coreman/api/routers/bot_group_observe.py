"""群聊旁听的判断记录：机器人管理员按群查看每次判断，给「会插话」的那些标注该不该说。

记录里有群聊原文摘录，只给机器人管理员看。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.db.models import Bot, BotMember, GroupObserveDecision, User

router = APIRouter(
    prefix="/api/admin/bots/{bot_id}/group-observe",
    tags=["bot-group-observe"],
    dependencies=[Depends(verify_csrf)],
)

# 「会插话」：模型判断该说话（shadow 下只记录，on 下发出或被 agent 否决）。
WOULD_SPEAK = ("interject", "sent", "vetoed", "failed")


async def require_admin(session: AsyncSession, bot_id: uuid.UUID, actor: User) -> Bot:
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    members = list(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot_id))
    )
    if actor.status != "active" or not is_bot_admin(actor, bot, members):
        raise ApiError(403, 403, "仅机器人管理员可查看旁听记录")
    return bot


def decision_out(row: GroupObserveDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "mode": row.mode,
        "outcome": row.outcome,
        "would_speak": row.outcome in WOULD_SPEAK,
        "probability": row.probability,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "messages": (row.state or {}).get("recent_messages") or [],
        "task_id": row.task_id,
        "review": row.review,
        "created_at": row.created_at,
    }


@router.get("/decisions")
async def list_decisions(
    bot_id: uuid.UUID,
    chat_id: str | None = None,
    would_speak: bool | None = None,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await require_admin(session, bot_id, user)
    stmt = select(GroupObserveDecision).where(GroupObserveDecision.bot_id == bot_id)
    if chat_id:
        stmt = stmt.where(GroupObserveDecision.chat_id == chat_id)
    if would_speak is not None:
        speak = GroupObserveDecision.outcome.in_(WOULD_SPEAK)
        stmt = stmt.where(speak if would_speak else ~speak)
    if before_id is not None:
        stmt = stmt.where(GroupObserveDecision.id < before_id)
    rows = list(await session.scalars(stmt.order_by(GroupObserveDecision.id.desc()).limit(limit)))
    chats = (
        await session.execute(
            select(
                GroupObserveDecision.chat_id,
                func.count(),
                func.count().filter(GroupObserveDecision.outcome.in_(WOULD_SPEAK)),
                func.max(GroupObserveDecision.created_at),
            )
            .where(GroupObserveDecision.bot_id == bot_id)
            .group_by(GroupObserveDecision.chat_id)
            .order_by(func.max(GroupObserveDecision.created_at).desc())
        )
    ).all()
    return {
        "code": 0,
        "data": {
            "items": [decision_out(row) for row in rows],
            "chats": [
                {"chat_id": cid, "total": total, "would_speak": speak, "last_at": last}
                for cid, total, speak, last in chats
            ],
        },
    }


class ReviewIn(BaseModel):
    review: Literal["should", "should_not"] | None


@router.patch("/decisions/{decision_id}")
async def review_decision(
    bot_id: uuid.UUID,
    decision_id: int,
    body: ReviewIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await require_admin(session, bot_id, user)
    row = await session.get(GroupObserveDecision, decision_id)
    if row is None or row.bot_id != bot_id:
        raise not_found("判断记录不存在")
    row.review = body.review
    await session.commit()
    return {"code": 0, "data": decision_out(row)}
