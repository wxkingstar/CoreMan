"""告警来源管理：绑定的服务用户会以自己的身份签发业务令牌，只有平台管理角色能改。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.permissions import is_manager
from coreman.core.db.models import Bot, BotAlertSource, User

router = APIRouter(
    prefix="/api/admin/bots/{bot_id}/alert-sources",
    tags=["bot-alert-sources"],
    dependencies=[Depends(verify_csrf)],
)


class AlertSourceIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    sender_union_id: str = Field(min_length=1, max_length=128)
    service_user_id: uuid.UUID
    enabled: bool = True
    dedupe_seconds: int = Field(default=600, ge=0, le=86400)


class AlertSourcePatch(BaseModel):
    service_user_id: uuid.UUID | None = None
    enabled: bool | None = None
    dedupe_seconds: int | None = Field(default=None, ge=0, le=86400)


def source_out(row: BotAlertSource) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "chat_id": row.chat_id,
        "sender_union_id": row.sender_union_id,
        "service_user_id": str(row.service_user_id),
        "enabled": row.enabled,
        "dedupe_seconds": row.dedupe_seconds,
        "created_at": row.created_at,
    }


async def _require(session: AsyncSession, bot_id: uuid.UUID, actor: User) -> Bot:
    if actor.status != "active" or not is_manager(actor):
        raise ApiError(403, 403, "仅平台管理员可管理告警来源")
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    if bot.platform != "feishu":
        raise ApiError(422, 422, "告警来源只支持飞书机器人")
    return bot


async def _check_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    user = await session.get(User, user_id)
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise ApiError(422, 422, "服务用户不存在或已停用")


async def _source(session: AsyncSession, bot_id: uuid.UUID, source_id: uuid.UUID) -> BotAlertSource:
    row = await session.get(BotAlertSource, source_id)
    if row is None or row.bot_id != bot_id:
        raise not_found("告警来源不存在")
    return row


async def _audit(
    session: AsyncSession, request: Request, user: User, action: str, row: BotAlertSource
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot_alert_source",
        target_id=str(row.id),
        diff={"bot_id": str(row.bot_id), "service_user_id": str(row.service_user_id)},
        ip=request.client.host if request.client else None,
    )


@router.get("")
async def list_sources(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require(session, bot_id, user)
    rows = await session.scalars(
        select(BotAlertSource)
        .where(BotAlertSource.bot_id == bot_id)
        .order_by(BotAlertSource.created_at)
    )
    return {"code": 0, "data": {"items": [source_out(r) for r in rows]}}


@router.post("")
async def create_source(
    bot_id: uuid.UUID,
    body: AlertSourceIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require(session, bot_id, user)
    await _check_user(session, body.service_user_id)
    exists = await session.scalar(
        select(BotAlertSource.id).where(
            BotAlertSource.bot_id == bot_id,
            BotAlertSource.chat_id == body.chat_id,
            BotAlertSource.sender_union_id == body.sender_union_id,
        )
    )
    if exists is not None:
        raise ApiError(409, 409, "这个告警来源已经登记")
    row = BotAlertSource(id=uuid.uuid4(), bot_id=bot_id, **body.model_dump())
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await _audit(session, request, user, "bot.alert_source.create", row)
    await session.commit()
    return {"code": 0, "data": source_out(row)}


@router.patch("/{source_id}")
async def update_source(
    bot_id: uuid.UUID,
    source_id: uuid.UUID,
    body: AlertSourcePatch,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require(session, bot_id, user)
    row = await _source(session, bot_id, source_id)
    changes = body.model_dump(exclude_none=True)
    if "service_user_id" in changes:
        await _check_user(session, changes["service_user_id"])
    for key, value in changes.items():
        setattr(row, key, value)
    await _audit(session, request, user, "bot.alert_source.update", row)
    await session.commit()
    return {"code": 0, "data": source_out(row)}


@router.delete("/{source_id}")
async def delete_source(
    bot_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require(session, bot_id, user)
    row = await _source(session, bot_id, source_id)
    await _audit(session, request, user, "bot.alert_source.delete", row)
    await session.delete(row)
    await session.commit()
    return {"code": 0, "data": {"deleted": str(source_id)}}
