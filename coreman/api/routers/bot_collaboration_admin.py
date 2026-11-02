"""AI 员工的协作设置：是否允许其他 AI 员工求助，以及可以求助的同事。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Exists, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers import feishu_apps as feishu_app_routes
from coreman.api.routers.bots import load_bot
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import record_audit
from coreman.core.chat import bot_collaboration as collaboration
from coreman.core.chat import collaboration_setup as setup
from coreman.core.chat import human_collaboration as human
from coreman.core.db.models import (
    Bot,
    BotCollaboration,
    BotCollaborationRoute,
    BotHumanPartner,
    HumanCollaboration,
    User,
    UserIdentity,
)
from coreman.core.platforms.feishu import FeishuError

router = APIRouter(
    prefix="/api/admin/bots", tags=["collaborators"], dependencies=[Depends(verify_csrf)]
)


class ToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


def incoming_active(bot_id: uuid.UUID):  # type: ignore[no-untyped-def]
    return (
        select(BotCollaboration)
        .join(BotCollaborationRoute, BotCollaboration.route_id == BotCollaborationRoute.id)
        .where(
            BotCollaborationRoute.target_bot_id == bot_id,
            BotCollaboration.status.in_(collaboration.ACTIVE),
        )
    )


async def bot_help_output(request: Request, session: AsyncSession, bot: Bot) -> dict[str, Any]:
    try:
        permission = await feishu_app_routes.collaboration_permission(request.app.state.cipher, bot)
    except (ApiError, FeishuError):
        # 手工创建、没开应用自管理权限的应用查不了权限清单：如实显示「无法检查」。
        permission = "unknown"
    return {
        "accept_bot_help": bot.accept_bot_help,
        "permission": permission,
        "active_count": await session.scalar(
            select(func.count()).select_from(incoming_active(bot.id).subquery())
        )
        or 0,
    }


@router.get("/{bot_id}/bot-help")
async def get_bot_help(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await source_bot(session, user, bot_id)
    return {"code": 0, "data": await bot_help_output(request, session, bot)}


@router.patch("/{bot_id}/bot-help")
async def set_bot_help(
    bot_id: uuid.UUID,
    body: ToggleIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """B 侧开关：关掉后其他 AI 员工搜不到它，进行中的求助一并取消。"""
    bot = await source_bot(session, user, bot_id, lock=True)
    if bot.accept_bot_help != body.enabled:
        bot.accept_bot_help = body.enabled
        if not body.enabled:
            rows = await session.scalars(
                incoming_active(bot.id)
                .order_by(BotCollaboration.id)
                .with_for_update(of=BotCollaboration)
            )
            for row in rows:
                await collaboration.close(
                    session, row, "cancelled", "对方已关闭「允许其他 AI 员工求助」"
                )
        await record_audit(
            session,
            action="bot.collaboration.accept_bot_help",
            actor_id=user.id,
            actor_login=user.login_name,
            target_type="bot",
            target_id=str(bot.id),
            diff={"accept_bot_help": body.enabled},
            ip=client_ip(request),
        )
        await session.flush()
    result = await bot_help_output(request, session, bot)
    await session.commit()
    return {"code": 0, "data": result}


# ---- Human colleagues ----------------------------------------------------------------------


class HumanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID
    responsibility: str = Field(default="", max_length=1000)


class HumanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    responsibility: str | None = Field(default=None, max_length=1000)


def _feishu_bound() -> Exists:
    return exists(
        select(UserIdentity.id).where(
            UserIdentity.user_id == User.id, UserIdentity.platform == "feishu"
        )
    )


async def human_output(session: AsyncSession, partner: BotHumanPartner) -> dict[str, Any]:
    person = await session.get(User, partner.user_id)
    bound = await human.feishu_identity(session, partner.user_id)
    units = await human.departments(session, [partner.user_id])
    reachable = bool(
        person and person.status == "active" and person.source != "bootstrap" and bound
    )
    return {
        "id": str(partner.id),
        "user_id": str(partner.user_id),
        "name": person.display_name if person else "已删除的成员",
        "position": (person.position if person else None) or "",
        "department": units.get(partner.user_id, ""),
        "responsibility": partner.responsibility,
        "enabled": partner.enabled,
        "version": partner.version,
        "reachable": reachable,
        "active_count": await session.scalar(
            select(func.count())
            .select_from(HumanCollaboration)
            .where(
                HumanCollaboration.partner_id == partner.id,
                HumanCollaboration.status.in_(human.WAITING),
            )
        )
        or 0,
    }


async def human_partner_for(
    session: AsyncSession, bot_id: uuid.UUID, partner_id: uuid.UUID
) -> BotHumanPartner:
    partner = await session.scalar(
        select(BotHumanPartner)
        .where(
            BotHumanPartner.id == partner_id,
            BotHumanPartner.source_bot_id == bot_id,
            BotHumanPartner.archived.is_(False),
        )
        .with_for_update()
    )
    if partner is None:
        raise not_found("协作同事不存在")
    return partner


async def human_audit(
    session: AsyncSession, request: Request, user: User, partner: BotHumanPartner, action: str
) -> None:
    await record_audit(
        session,
        action=f"bot.collaboration.human_{action}",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(partner.source_bot_id),
        diff={
            "partner_id": str(partner.id),
            "user_id": str(partner.user_id),
            "enabled": partner.enabled,
        },
        ip=client_ip(request),
    )


@router.get("/{bot_id}/human-collaborators")
async def list_humans(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    partners = await session.scalars(
        select(BotHumanPartner)
        .where(BotHumanPartner.source_bot_id == bot_id, BotHumanPartner.archived.is_(False))
        .order_by(BotHumanPartner.created_at, BotHumanPartner.id)
    )
    return {"code": 0, "data": [await human_output(session, p) for p in partners]}


@router.get("/{bot_id}/human-collaborator-options")
async def human_options(
    bot_id: uuid.UUID,
    q: str = Query(default="", max_length=100),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id)
    # Only people the Feishu app can reach; same searchable fields as the member directory.
    query = select(User).where(User.status == "active", User.source != "bootstrap", _feishu_bound())
    if q.strip():
        query = query.where(
            or_(
                User.display_name.icontains(q.strip(), autoescape=True),
                User.login_name.icontains(q.strip(), autoescape=True),
            )
        )
    people = list(await session.scalars(query.order_by(User.display_name, User.id).limit(50)))
    units = await human.departments(session, [p.id for p in people])
    added = set(
        await session.scalars(
            select(BotHumanPartner.user_id).where(
                BotHumanPartner.source_bot_id == bot_id, BotHumanPartner.archived.is_(False)
            )
        )
    )
    return {
        "code": 0,
        "data": [
            {
                "id": str(p.id),
                "name": p.display_name,
                "login_name": p.login_name,
                "position": p.position or "",
                "department": units.get(p.id, ""),
                "added": p.id in added,
            }
            for p in people
        ],
    }


@router.post("/{bot_id}/human-collaborators", status_code=201)
async def create_human(
    bot_id: uuid.UUID,
    body: HumanIn,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id, lock=True)
    person = await session.get(User, body.user_id)
    if (
        person is None
        or person.status != "active"
        or person.source == "bootstrap"
        or await human.feishu_identity(session, person.id) is None
    ):
        raise ApiError(422, 422, "请选择已绑定飞书账号的在职成员")
    partner = await session.scalar(
        select(BotHumanPartner)
        .where(BotHumanPartner.source_bot_id == bot_id, BotHumanPartner.user_id == person.id)
        .with_for_update()
    )
    if partner is not None and not partner.archived:
        raise ApiError(409, 409, "该同事已添加，请直接在列表中操作")
    if partner is None:
        partner = BotHumanPartner(
            source_bot_id=bot_id,
            user_id=person.id,
            responsibility=body.responsibility.strip(),
            enabled=True,
            archived=False,
            version=1,
        )
        session.add(partner)
    else:
        partner.archived, partner.enabled = False, True
        partner.responsibility = body.responsibility.strip()
    await session.flush()
    await human_audit(session, request, user, partner, "add")
    result = await human_output(session, partner)
    set_etag(response, partner.version)
    await session.commit()
    return {"code": 0, "data": result}


@router.patch("/{bot_id}/human-collaborators/{partner_id}")
async def update_human(
    bot_id: uuid.UUID,
    partner_id: uuid.UUID,
    body: HumanPatch,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await source_bot(session, user, bot_id, lock=True)
    partner = await human_partner_for(session, bot_id, partner_id)
    require_if_match(request, partner.version)
    action = "edit"
    if body.responsibility is not None:
        partner.responsibility = body.responsibility.strip()
    if body.enabled is not None and body.enabled != partner.enabled:
        partner.enabled = body.enabled
        action = "enable" if body.enabled else "pause"
        if not body.enabled:
            await human.cancel_partner(session, partner, "管理员已暂停向该同事求助")
    await session.flush()
    await human_audit(session, request, user, partner, action)
    result = await human_output(session, partner)
    set_etag(response, partner.version)
    await session.commit()
    return {"code": 0, "data": result}


@router.delete("/{bot_id}/human-collaborators/{partner_id}", status_code=204)
async def archive_human(
    bot_id: uuid.UUID,
    partner_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await source_bot(session, user, bot_id, lock=True)
    partner = await human_partner_for(session, bot_id, partner_id)
    require_if_match(request, partner.version)
    await human.cancel_partner(session, partner, "管理员已移除该协作同事")
    partner.archived = True
    await human_audit(session, request, user, partner, "remove")
    await session.commit()
    return Response(status_code=204)
