"""系统授权与基础设施凭证管理；所有密钥只在创建或轮换时返回一次。"""

from __future__ import annotations

import secrets
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.infra_auth import INFRA_SCOPES
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.routers.bots import load_bot, member_ids_of
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import record_audit
from coreman.core.auth.system_access import RESERVED_SYSTEM_KEYS
from coreman.core.auth.tokens import active_key, public_keys
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.db.models import (
    ApiClient,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    JwtKey,
    SystemGrantAudit,
    User,
)

router = APIRouter(
    prefix="/api/admin", tags=["infrastructure"], dependencies=[Depends(verify_csrf)]
)
public_router = APIRouter(tags=["jwks"])
MANAGERS = require_roles("ai_committee", "platform_admin")
ADMINS = require_roles("platform_admin")


class SystemIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    base_url: str | None = Field(default=None, max_length=2048)
    sitemap_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    sort_order: int = 0
    default_for_all_bots: bool = False
    # 空列表 = 显式白名单且暂无机器人；null = 对全部机器人开放，必须由管理员明确选择。
    # 缺省按空白名单处理：bot 管理员能控制提示词与 env，而平台会把发言者令牌注入其 CLI，
    # 新系统不能在没人确认的情况下对所有机器人开放。
    allowed_bot_ids: list[uuid.UUID] | None = Field(default_factory=list, max_length=500)

    @field_validator("base_url", "sitemap_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value:
            url = urlsplit(value)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("请输入不带凭证的 HTTP(S) 地址")
        return value


class SystemCreate(SystemIn):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")

    @field_validator("key")
    @classmethod
    def not_reserved(cls, value: str) -> str:
        if value in RESERVED_SYSTEM_KEYS:
            raise ValueError("该标识为平台保留（与 CoreMan 自身令牌的 audience 冲突），请换一个")
        return value


class GrantsIn(BaseModel):
    system_keys: list[str] = Field(max_length=100)
    comment: str = Field(default="", max_length=2000)


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        if not set(value) <= INFRA_SCOPES:
            raise ValueError("包含未知接口组")
        return sorted(set(value))


class ClientCreate(ClientIn):
    app_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,128}$")
    secret: str | None = Field(default=None, min_length=32, max_length=512)


def system_out(row: BusinessSystem) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "key",
            "name",
            "description",
            "base_url",
            "sitemap_url",
            "enabled",
            "sort_order",
            "default_for_all_bots",
            "allowed_bot_ids",
            "version",
        )
    }


def client_out(row: ApiClient) -> dict[str, Any]:
    return {
        "app_key": row.app_key,
        "name": row.name,
        "scopes": row.scopes,
        "enabled": row.enabled,
        "version": row.version,
        "last_used_at": row.last_used_at,
        "has_secret": True,
    }


async def persist(session: AsyncSession, *, commit: bool = False) -> None:
    try:
        if commit:
            await session.commit()
        else:
            await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, 409, "记录已存在或关联数据已变化，请刷新后重试") from exc


async def audit(
    session: AsyncSession,
    request: Request,
    user: User,
    action: str,
    kind: str,
    target: str,
    diff: dict[str, Any] | None = None,
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=user.id,
        actor_login=user.login_name,
        target_type=kind,
        target_id=target,
        diff=diff,
        ip=client_ip(request),
    )


async def check_bots(session: AsyncSession, ids: list[uuid.UUID] | None) -> None:
    if ids:
        found = set((await session.execute(select(Bot.id).where(Bot.id.in_(ids)))).scalars())
        if set(ids) != found:
            raise ApiError(422, 422, "白名单包含不存在的机器人")


@router.get("/systems")
async def list_systems(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    page: PageParams = Depends(),
) -> dict[str, Any]:
    data = await paginate(
        session,
        select(BusinessSystem).order_by(BusinessSystem.sort_order, BusinessSystem.key),
        page,
    )
    data["items"] = [system_out(row) for row in data["items"]]
    return {"code": 0, "data": data}


@router.post("/systems", status_code=201)
async def create_system(
    body: SystemCreate,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if await session.get(BusinessSystem, body.key):
        raise ApiError(409, 409, "系统标识已存在")
    await check_bots(session, body.allowed_bot_ids)
    row = BusinessSystem(**body.model_dump())
    session.add(row)
    await persist(session)
    await audit(session, request, user, "system.create", "system", row.key)
    await persist(session, commit=True)
    return {"code": 0, "data": system_out(row)}


@router.put("/systems/{key}")
async def update_system(
    key: str,
    body: SystemIn,
    request: Request,
    response: Response,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(BusinessSystem).where(BusinessSystem.key == key).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("系统不存在")
    require_if_match(request, row.version)
    await check_bots(session, body.allowed_bot_ids)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    # 白名单变窄时实际删除授权，之后放开也不会悄悄恢复历史授权。
    if body.allowed_bot_ids is not None:
        removed = list(
            (
                await session.execute(
                    delete(BotSystemGrant)
                    .where(
                        BotSystemGrant.system_key == key,
                        BotSystemGrant.bot_id.not_in(body.allowed_bot_ids),
                    )
                    .returning(BotSystemGrant.bot_id)
                )
            ).scalars()
        )
        for bot_id in removed:
            session.add(
                SystemGrantAudit(
                    bot_id=bot_id,
                    requested=[key],
                    approved=[],
                    actor_id=user.id,
                    comment="系统白名单收紧",
                )
            )
    await audit(session, request, user, "system.update", "system", key)
    await persist(session, commit=True)
    set_etag(response, row.version)
    return {"code": 0, "data": system_out(row)}


@router.delete("/systems/{key}")
async def delete_system(
    key: str,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(BusinessSystem, key)
    if row is None:
        raise not_found("系统不存在")
    require_if_match(request, row.version)
    await session.delete(row)
    await audit(session, request, user, "system.delete", "system", key)
    await persist(session, commit=True)
    return {"code": 0, "data": None}


@router.get("/bots/{bot_id}/system-grants")
async def get_grants(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not is_bot_admin(user, bot, await member_ids_of(session, bot_id)):
        raise forbidden()
    keys = list(
        (
            await session.execute(
                select(BotSystemGrant.system_key)
                .where(BotSystemGrant.bot_id == bot_id)
                .order_by(BotSystemGrant.system_key)
            )
        ).scalars()
    )
    return {"code": 0, "data": {"system_keys": keys, "version": bot.version}}


@router.put("/bots/{bot_id}/system-grants")
async def put_grants(
    bot_id: uuid.UUID,
    body: GrantsIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not is_bot_admin(user, bot, await member_ids_of(session, bot_id)):
        raise forbidden()
    require_if_match(request, bot.version)
    keys = sorted(set(body.system_keys))
    systems = list(
        (
            await session.execute(
                select(BusinessSystem)
                .where(BusinessSystem.key.in_(keys))
                .order_by(BusinessSystem.key)
                .with_for_update()
            )
        ).scalars()
    )
    # allowed_bot_ids 为 null 的历史行仍按「对全部机器人开放」处理（不做数据迁移），
    # 管理台会明示这一状态；新建系统缺省是空白名单。
    if len(systems) != len(keys) or any(
        not s.enabled
        or s.key in RESERVED_SYSTEM_KEYS
        or (s.allowed_bot_ids is not None and bot_id not in s.allowed_bot_ids)
        for s in systems
    ):
        raise ApiError(403, 403, "申请包含未开放给此机器人的系统")
    await session.execute(delete(BotSystemGrant).where(BotSystemGrant.bot_id == bot_id))
    session.add_all(
        [BotSystemGrant(bot_id=bot_id, system_key=key, granted_by=user.id) for key in keys]
    )
    session.add(
        SystemGrantAudit(
            bot_id=bot_id,
            requested=body.system_keys,
            approved=keys,
            actor_id=user.id,
            comment=body.comment,
        )
    )
    bot.version += 1
    await persist(session)
    await notify_bot_changed(session, bot.id)
    await audit(session, request, user, "bot.system_grants", "bot", str(bot_id))
    await persist(session, commit=True)
    return {"code": 0, "data": {"system_keys": keys, "version": bot.version}}


@router.get("/api-clients")
async def list_clients(
    user: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
    page: PageParams = Depends(),
) -> dict[str, Any]:
    data = await paginate(session, select(ApiClient).order_by(ApiClient.app_key), page)
    data["items"] = [client_out(row) for row in data["items"]]
    return {"code": 0, "data": data}


@router.post("/api-clients", status_code=201)
async def create_client(
    body: ClientCreate,
    request: Request,
    user: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if await session.get(ApiClient, body.app_key):
        raise ApiError(409, 409, "调用方标识已存在")
    secret = body.secret or secrets.token_urlsafe(48)
    row = ApiClient(
        **body.model_dump(exclude={"secret"}),
        secret_enc=request.app.state.cipher.encrypt(secret, "api_clients.secret_enc"),
    )
    session.add(row)
    await persist(session)
    await audit(session, request, user, "api_client.create", "api_client", row.app_key)
    await persist(session, commit=True)
    return {"code": 0, "data": {**client_out(row), "secret": secret}}


@router.put("/api-clients/{key}")
async def update_client(
    key: str,
    body: ClientIn,
    request: Request,
    user: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(ApiClient, key)
    if row is None:
        raise not_found("调用方不存在")
    require_if_match(request, row.version)
    for name, value in body.model_dump().items():
        setattr(row, name, value)
    await audit(session, request, user, "api_client.update", "api_client", key)
    await persist(session, commit=True)
    return {"code": 0, "data": client_out(row)}


@router.post("/api-clients/{key}/rotate-secret")
async def rotate_client(
    key: str,
    request: Request,
    user: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(ApiClient, key)
    if row is None:
        raise not_found("调用方不存在")
    require_if_match(request, row.version)
    secret = secrets.token_urlsafe(48)
    row.secret_enc = request.app.state.cipher.encrypt(secret, "api_clients.secret_enc")
    await audit(session, request, user, "api_client.rotate", "api_client", key)
    await persist(session, commit=True)
    return {"code": 0, "data": {**client_out(row), "secret": secret}}


@router.get("/jwt-keys")
async def list_keys(
    user: User = Depends(ADMINS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (await session.execute(select(JwtKey).order_by(JwtKey.created_at.desc()))).scalars()
    return {
        "code": 0,
        "data": [
            {
                "kid": r.kid,
                "is_active": r.is_active,
                "created_at": r.created_at,
                "retired_at": r.retired_at,
            }
            for r in rows
        ],
    }


@router.post("/jwt-keys/rotate")
async def rotate_key(
    request: Request, user: User = Depends(ADMINS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    key = await active_key(session, request.app.state.cipher, rotate=True)
    await audit(session, request, user, "jwt_key.rotate", "jwt_key", key.kid)
    await persist(session, commit=True)
    return {"code": 0, "data": {"kid": key.kid, "public_jwk": key.public_jwk}}


@public_router.get("/api/.well-known/jwks.json")
async def jwks(response: Response, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"keys": await public_keys(session)}
