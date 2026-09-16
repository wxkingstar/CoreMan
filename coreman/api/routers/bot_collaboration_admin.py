"""Source-managed partner selection, independent of runtime group verification."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.bots import load_bot
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import record_audit
from coreman.core.bus import outbox
from coreman.core.chat import bot_collaboration as collaboration
from coreman.core.chat import collaboration_setup as setup
from coreman.core.db.models import (
    Bot,
    BotCollaboration,
    BotCollaborationPartner,
    BotCollaborationRoute,
    OutboxItem,
    User,
)

router = APIRouter(
    prefix="/api/admin/bots", tags=["collaborators"], dependencies=[Depends(verify_csrf)]
)


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_bot_id: uuid.UUID


class ToggleIn(BaseModel):
    enabled: bool


async def source_bot(
    session: AsyncSession, user: User, bot_id: uuid.UUID, *, lock: bool = False
) -> Bot:
    bot = (
        await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
        if lock
        else await load_bot(session, bot_id)
    )
    if bot is None:
        raise not_found("协作伙伴或 AI 员工不存在")
    if not await setup.manageable(session, user, bot):
        raise forbidden()
    if bot.platform != "feishu":
        raise ApiError(422, 422, "当前仅支持飞书 AI 员工协作")
    return bot


async def target_bot(session: AsyncSession, source: Bot, target_id: uuid.UUID) -> Bot:
    if source.id == target_id:
        raise ApiError(422, 422, "不能将自己添加为协作伙伴")
    target = await load_bot(session, target_id)
    if target.platform != "feishu":
        raise ApiError(422, 422, "请选择飞书 AI 员工作为协作伙伴")
    # Selecting a partner grants no target management or human-use permission.
    return target


async def partner_for(
    session: AsyncSession, bot_id: uuid.UUID, partner_id: uuid.UUID
) -> BotCollaborationPartner:
    partner = await session.scalar(
        select(BotCollaborationPartner)
        .where(
            BotCollaborationPartner.id == partner_id,
            BotCollaborationPartner.source_bot_id == bot_id,
            BotCollaborationPartner.archived.is_(False),
        )
        .with_for_update()
    )
    if partner is None:
        raise not_found("协作伙伴或 AI 员工不存在")
    return partner


def active_query(partner: BotCollaborationPartner):  # type: ignore[no-untyped-def]
    return (
        select(BotCollaboration)
        .join(BotCollaborationRoute, BotCollaboration.route_id == BotCollaborationRoute.id)
        .where(
            BotCollaborationRoute.source_bot_id == partner.source_bot_id,
            BotCollaborationRoute.target_bot_id == partner.target_bot_id,
            BotCollaboration.status.in_(collaboration.ACTIVE),
        )
    )


async def output(session: AsyncSession, partner: BotCollaborationPartner) -> dict[str, Any]:
    target = await session.get(Bot, partner.target_bot_id)
    available = bool(target and await setup.available(session, target))
    await session.flush()
    return {
        "id": str(partner.id),
        "target_bot_id": str(partner.target_bot_id),
        "target_name": target.name if target else "已移除的 AI 员工",
        "target_description": target.description if target else "",
        "enabled": partner.enabled,
        "version": partner.version,
        "status": "ready" if available else "unavailable",
        "reason": None if available else "runtime_unavailable",
        "can_enable": True,
        "can_remove": True,
        "active_count": await session.scalar(
            select(func.count()).select_from(active_query(partner).subquery())
        )
        or 0,
    }


async def audit(
    session: AsyncSession,
    request: Request,
    user: User,
    partner: BotCollaborationPartner,
    action: str,
) -> None:
    await record_audit(
        session,
        action=f"bot.collaboration.{action}",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(partner.source_bot_id),
        diff={
            "partner_id": str(partner.id),
            "target_bot_id": str(partner.target_bot_id),
            "enabled": partner.enabled,
        },
        ip=client_ip(request),
    )


@router.get("/{bot_id}/collaborators")
async def list_partners(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    partners = await session.scalars(
        select(BotCollaborationPartner)
        .where(
            BotCollaborationPartner.source_bot_id == bot_id,
            BotCollaborationPartner.archived.is_(False),
        )
        .order_by(BotCollaborationPartner.id)
    )
    return {"code": 0, "data": [await output(session, p) for p in partners]}


@router.get("/{bot_id}/collaborator-options")
async def options(
    bot_id: uuid.UUID,
    q: str = Query(default="", max_length=100),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    query = select(Bot).where(Bot.id != bot_id, Bot.platform == "feishu")
    if q.strip():
        query = query.where(
            or_(
                Bot.name.icontains(q.strip(), autoescape=True),
                Bot.description.icontains(q.strip(), autoescape=True),
            )
        )
    bots = await session.scalars(query.order_by(Bot.name, Bot.id).limit(50))
    return {
        "code": 0,
        "data": [
            {
                "id": str(bot.id),
                "name": bot.name,
                "description": bot.description,
                "enabled": bot.enabled,
                "available": await setup.available(session, bot),
            }
            for bot in bots
        ],
    }


@router.post("/{bot_id}/collaborators", status_code=201)
async def create(
    bot_id: uuid.UUID,
    body: CreateIn,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await source_bot(session, user, bot_id, lock=True)
    target = await target_bot(session, source, body.target_bot_id)
    partner = await session.scalar(
        select(BotCollaborationPartner)
        .where(
            BotCollaborationPartner.source_bot_id == bot_id,
            BotCollaborationPartner.target_bot_id == target.id,
        )
        .with_for_update()
    )
    if partner is not None and not partner.archived:
        raise ApiError(409, 409, "该协作伙伴已添加，请直接在列表中操作")
    if partner is None:
        partner = BotCollaborationPartner(
            source_bot_id=bot_id, target_bot_id=target.id, enabled=True, archived=False, version=1
        )
        session.add(partner)
    else:
        partner.archived, partner.enabled = False, True
    await session.flush()
    await audit(session, request, user, partner, "add")
    result = await output(session, partner)
    set_etag(response, partner.version)
    await session.commit()
    return {"code": 0, "data": result}


async def stop(session: AsyncSession, partner: BotCollaborationPartner) -> None:
    # The scheduler holds ledger before reconciling route proof. Match that order.
    rows = list(
        await session.scalars(
            active_query(partner).order_by(BotCollaboration.id).with_for_update(of=BotCollaboration)
        )
    )
    partner.enabled = False
    routes = await session.scalars(
        select(BotCollaborationRoute)
        .where(
            BotCollaborationRoute.source_bot_id == partner.source_bot_id,
            BotCollaborationRoute.target_bot_id == partner.target_bot_id,
        )
        .order_by(BotCollaborationRoute.id)
        .with_for_update()
    )
    for route in routes:
        route.enabled = False
        if route.setup.get("status") == "pending":
            route.setup = {**route.setup, "status": "failed", "reason": "verification_cancelled"}
            for side in ("source", "target"):
                probe_id = route.setup.get(f"{side}_outbox_id")
                probe = await session.get(OutboxItem, probe_id) if probe_id else None
                if probe and probe.status == "pending":
                    await outbox.mark_skipped(session, probe.id, "协作伙伴已暂停或移除")
    for row in rows:
        await collaboration.close(session, row, "cancelled", "管理员已暂停或移除协作伙伴")


@router.patch("/{bot_id}/collaborators/{partner_id}")
async def toggle(
    bot_id: uuid.UUID,
    partner_id: uuid.UUID,
    body: ToggleIn,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await source_bot(session, user, bot_id, lock=True)
    partner = await partner_for(session, bot_id, partner_id)
    require_if_match(request, partner.version)
    if body.enabled:
        await target_bot(session, source, partner.target_bot_id)
        partner.enabled = True
    else:
        await stop(session, partner)
    await audit(session, request, user, partner, "enable" if body.enabled else "pause")
    result = await output(session, partner)
    set_etag(response, partner.version)
    await session.commit()
    return {"code": 0, "data": result}


@router.delete("/{bot_id}/collaborators/{partner_id}", status_code=204)
async def archive(
    bot_id: uuid.UUID,
    partner_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await source_bot(session, user, bot_id, lock=True)
    partner = await partner_for(session, bot_id, partner_id)
    require_if_match(request, partner.version)
    await stop(session, partner)
    partner.archived = True
    await audit(session, request, user, partner, "remove")
    await session.commit()
    return Response(status_code=204)
