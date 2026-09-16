"""Human-managed, verified outgoing collaboration connections."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.bots import load_bot
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import record_audit
from coreman.core.chat import bot_collaboration as collaboration
from coreman.core.chat import collaboration_setup as setup
from coreman.core.db.models import Bot, BotCollaboration, BotCollaborationRoute, BotMember, User
from coreman.core.platforms.feishu import FeishuError

router = APIRouter(
    prefix="/api/admin/bots", tags=["collaborators"], dependencies=[Depends(verify_csrf)]
)


class CreateIn(BaseModel):
    target_bot_id: uuid.UUID
    chat_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")


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
        raise not_found("协作连接或 AI 员工不存在")
    if not await setup.manageable(session, user, bot):
        raise forbidden()
    return bot


async def target_bot(session: AsyncSession, user: User, source: Bot, target_id: uuid.UUID) -> Bot:
    if source.id == target_id:
        raise ApiError(422, 422, "不能将自己添加为协作伙伴")
    target = await load_bot(session, target_id)
    if not await setup.manageable(session, user, target):
        raise forbidden()
    if not await setup.available(session, source) or not await setup.available(session, target):
        raise ApiError(422, 422, "请先启用双方飞书 AI 员工并配置可用的 Runtime")
    return target


async def route_for(
    session: AsyncSession, bot_id: uuid.UUID, route_id: uuid.UUID
) -> BotCollaborationRoute:
    route = await session.scalar(
        select(BotCollaborationRoute)
        .where(
            BotCollaborationRoute.id == route_id,
            BotCollaborationRoute.source_bot_id == bot_id,
            BotCollaborationRoute.archived.is_(False),
        )
        .with_for_update()
    )
    if route is None:
        raise not_found("协作连接或 AI 员工不存在")
    return route


@asynccontextmanager
async def platform_read():  # type: ignore[no-untyped-def]
    try:
        async with asyncio.timeout(30):
            yield
    except (FeishuError, httpx.HTTPError, TimeoutError) as from_exc:
        raise ApiError(
            502, 502, "无法完成飞书连接检查，请检查双方应用凭证、群权限和网络后重试"
        ) from from_exc
    except ValueError as exc:
        raise ApiError(422, 422, "连接验证条件不满足，请检查双方应用凭证和所选群后重试") from exc


async def output(session: AsyncSession, route: BotCollaborationRoute, user: User) -> dict[str, Any]:
    target = await session.get(Bot, route.target_bot_id)
    state = await setup.status(session, route)
    await session.flush()
    can_manage = target is not None and await setup.manageable(session, user, target)
    return {
        "id": str(route.id),
        "target_bot_id": str(route.target_bot_id),
        "target_name": target.name if target else "已移除的 AI 员工",
        "target_description": target.description if target else "",
        "chat_id": route.chat_id,
        "chat_name": route.setup.get("chat_name") or "已配置的飞书群",
        "enabled": route.enabled,
        "version": route.version,
        "status": state["status"],
        "reason": state.get("reason"),
        "can_enable": bool(state.get("can_enable") and can_manage),
        "can_verify": bool(can_manage and not route.enabled and state["status"] != "pending"),
        "can_remove": True,
        "active_count": await session.scalar(
            select(func.count())
            .select_from(BotCollaboration)
            .where(
                BotCollaboration.route_id == route.id,
                BotCollaboration.status.in_(collaboration.ACTIVE),
            )
        )
        or 0,
    }


async def audit(
    session: AsyncSession, request: Request, user: User, route: BotCollaborationRoute, action: str
) -> None:
    await record_audit(
        session,
        action=f"bot.collaboration.{action}",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(route.source_bot_id),
        diff={
            "route_id": str(route.id),
            "target_bot_id": str(route.target_bot_id),
            "enabled": route.enabled,
        },
        ip=client_ip(request),
    )


@router.get("/{bot_id}/collaborators")
async def list_routes(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    routes = list(
        await session.scalars(
            select(BotCollaborationRoute)
            .where(
                BotCollaborationRoute.source_bot_id == bot_id,
                BotCollaborationRoute.archived.is_(False),
            )
            .order_by(BotCollaborationRoute.id)
            .with_for_update()
        )
    )
    for route in routes:
        await setup.reconcile(session, route)
    result = [await output(session, route, user) for route in routes]
    await session.commit()
    return {"code": 0, "data": result}


@router.get("/{bot_id}/collaborator-options")
async def options(
    bot_id: uuid.UUID,
    q: str = Query(default="", max_length=100),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    managed = select(BotMember.bot_id).where(BotMember.user_id == user.id)
    query = select(Bot).where(
        Bot.id != bot_id,
        Bot.platform == "feishu",
        or_(Bot.created_by == user.id, Bot.id.in_(managed)),
    )
    if q.strip():
        query = query.where(
            or_(
                Bot.name.icontains(q.strip(), autoescape=True),
                Bot.description.icontains(q.strip(), autoescape=True),
            )
        )
    bots = await session.scalars(query.order_by(Bot.name, Bot.id).limit(50))
    result = [
        {
            "id": str(bot.id),
            "name": bot.name,
            "description": bot.description,
            "enabled": bot.enabled,
            "available": await setup.available(session, bot),
        }
        for bot in bots
    ]
    return {"code": 0, "data": result}


@router.get("/{bot_id}/collaborator-groups")
async def groups(
    bot_id: uuid.UUID,
    target_bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await source_bot(session, user, bot_id)
    target = await target_bot(session, user, source, target_bot_id)
    async with platform_read():
        result = await setup.common_groups(source, target, request.app.state.cipher)
    return {"code": 0, "data": result}


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
    target = await target_bot(session, user, source, body.target_bot_id)
    route = await session.scalar(
        select(BotCollaborationRoute)
        .where(
            BotCollaborationRoute.source_bot_id == bot_id,
            BotCollaborationRoute.target_bot_id == target.id,
            BotCollaborationRoute.chat_id == body.chat_id,
        )
        .with_for_update()
    )
    if route is not None and not route.archived:
        raise ApiError(409, 409, "该伙伴在此群的连接已存在，请直接在列表中操作")
    async with platform_read():
        if route is None:
            route = BotCollaborationRoute(
                source_bot_id=bot_id,
                target_bot_id=target.id,
                chat_id=body.chat_id,
                tenant_key="",
                source_open_id="",
                target_open_id="",
                source_union_id="",
                target_union_id="",
                enabled=False,
                archived=False,
                setup={},
                version=1,
            )
            session.add(route)
            await session.flush()
        else:
            route.archived = False
        await setup.begin(session, route, source, target, user, request.app.state.cipher)
    await audit(session, request, user, route, "verify")
    result = await output(session, route, user)
    set_etag(response, route.version)
    await session.commit()
    return {"code": 0, "data": result}


@router.post("/{bot_id}/collaborators/{route_id}/verify")
async def verify(
    bot_id: uuid.UUID,
    route_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await source_bot(session, user, bot_id, lock=True)
    route = await route_for(session, bot_id, route_id)
    require_if_match(request, route.version)
    target = await target_bot(session, user, source, route.target_bot_id)
    await setup.reconcile(session, route)
    if route.enabled or route.setup.get("status") == "pending":
        raise ApiError(409, 409, "请先暂停连接或等待当前验证结束")
    async with platform_read():
        await setup.begin(session, route, source, target, user, request.app.state.cipher)
    await audit(session, request, user, route, "verify")
    result = await output(session, route, user)
    set_etag(response, route.version)
    await session.commit()
    return {"code": 0, "data": result}


async def stop(session: AsyncSession, route: BotCollaborationRoute) -> None:
    route.enabled = False
    if route.setup.get("status") == "pending":
        route.setup = {**route.setup, "status": "failed", "reason": "verification_cancelled"}
    rows = await session.scalars(
        select(BotCollaboration)
        .where(
            BotCollaboration.route_id == route.id, BotCollaboration.status.in_(collaboration.ACTIVE)
        )
        .order_by(BotCollaboration.id)
        .with_for_update()
    )
    for row in rows:
        await collaboration.close(session, row, "cancelled", "管理员已暂停或移除协作连接")


@router.patch("/{bot_id}/collaborators/{route_id}")
async def toggle(
    bot_id: uuid.UUID,
    route_id: uuid.UUID,
    body: ToggleIn,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await source_bot(session, user, bot_id, lock=True)
    route = await route_for(session, bot_id, route_id)
    require_if_match(request, route.version)
    if body.enabled:
        await target_bot(session, user, source, route.target_bot_id)
        await setup.reconcile(session, route)
        state = await setup.status(session, route)
        if not state.get("can_enable"):
            raise ApiError(422, 422, "连接尚未通过验证或配置已变化，请重新验证")
        route.enabled = True
    else:
        await stop(session, route)
    await audit(session, request, user, route, "enable" if body.enabled else "pause")
    result = await output(session, route, user)
    set_etag(response, route.version)
    await session.commit()
    return {"code": 0, "data": result}


@router.delete("/{bot_id}/collaborators/{route_id}", status_code=204)
async def archive(
    bot_id: uuid.UUID,
    route_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await source_bot(session, user, bot_id, lock=True)
    route = await route_for(session, bot_id, route_id)
    require_if_match(request, route.version)
    await stop(session, route)
    route.archived = True
    await audit(session, request, user, route, "remove")
    await session.commit()
    return Response(status_code=204)
