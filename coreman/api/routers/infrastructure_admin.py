"""系统授权与基础设施凭证管理；所有密钥只在创建或轮换时返回一次。"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.infra_auth import INFRA_SCOPES, RETIRED_INFRA_SCOPES
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.routers.bots import load_bot, member_ids_of
from coreman.api.routers.systems_catalog import refresh_as
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import diff_dict, record_audit
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
    User,
)
from coreman.core.systems_catalog.compiler import same_origin

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
    openapi_url: str | None = Field(default=None, max_length=2048)
    # openapi_url 的旧名，本版本仍接受；两者都给时以 openapi_url 为准。
    sitemap_url: str | None = Field(default=None, max_length=2048, exclude=True)
    token_provider: str = Field(default="builtin", min_length=1, max_length=100)
    # env：令牌下发到运行环境；proxy：只经平台代理调用（systems_call），令牌不进运行环境。
    token_delivery: Literal["env", "proxy"] = "env"
    token_audience: str | None = Field(default=None, max_length=2048)
    access_test_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    sort_order: int = 0
    default_for_all_bots: bool = False
    # 空列表 = 显式白名单且暂无机器人；null = 对全部机器人开放，必须由管理员明确选择。
    # 缺省按空白名单处理：bot 管理员能控制提示词与 env，而平台会把发言者令牌注入其 CLI，
    # 新系统不能在没人确认的情况下对所有机器人开放。
    allowed_bot_ids: list[uuid.UUID] | None = Field(default_factory=list, max_length=500)

    @field_validator(
        "token_audience", "access_test_url", "openapi_url", "sitemap_url", mode="before"
    )
    @classmethod
    def normalize_optional(cls, value: Any) -> Any:
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def legacy_openapi_url(self) -> SystemIn:
        if "openapi_url" not in self.model_fields_set and "sitemap_url" in self.model_fields_set:
            self.openapi_url = self.sitemap_url
            self.model_fields_set.add("openapi_url")
        return self

    def row_values(self) -> dict[str, Any]:
        """写库的字段：旧列 sitemap_url 与 openapi_url 同值，滚动升级中的旧进程读到的也一样。"""
        values = self.model_dump()
        values["sitemap_url"] = values["openapi_url"]
        return values

    @field_validator("base_url", "openapi_url", "sitemap_url", "access_test_url")
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
    # 允许平台代理执行 write 级操作的系统，须是 system_keys 的子集；省略时保留原设置。
    write_keys: list[str] | None = Field(default=None, max_length=100)
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
        # 已下线的接口组静默丢弃：旧客户端回传原有勾选时不能因此保存失败。
        current = set(value) - RETIRED_INFRA_SCOPES
        if not current <= INFRA_SCOPES:
            raise ValueError("包含未知接口组")
        return sorted(current)


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
            "openapi_url",
            "token_delivery",
            "token_provider",
            "token_audience",
            "access_test_url",
            "enabled",
            "sort_order",
            "default_for_all_bots",
            "allowed_bot_ids",
            "version",
        )
    } | {"sitemap_url": row.openapi_url}


def client_out(row: ApiClient) -> dict[str, Any]:
    return {
        "app_key": row.app_key,
        "name": row.name,
        # 旧数据里可能还存着已下线的接口组，读取时不展示也不报错。
        "scopes": [scope for scope in row.scopes if scope in INFRA_SCOPES],
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


def validate_provider_settings(
    request: Request, values: dict[str, Any], previous: str | None = None
) -> None:
    if values.get("token_audience") in RESERVED_SYSTEM_KEYS:
        raise ApiError(422, 422, "令牌受众为平台保留标识")
    provider = values["token_provider"]
    configured = request.app.state.settings.business_token_providers
    if provider != "builtin" and provider != previous and provider not in configured:
        raise ApiError(422, 422, "未知令牌签发方")
    openapi_url = values.get("openapi_url")
    if openapi_url and (
        not same_origin(openapi_url, values["base_url"] or "") or urlsplit(openapi_url).fragment
    ):
        raise ApiError(422, 422, "OpenAPI 地址必须与系统地址同源且不含片段")
    test_url = values["access_test_url"]
    if test_url:
        base = urlsplit(values["base_url"] or "")
        probe = urlsplit(test_url)
        try:
            base_origin = (
                base.scheme,
                base.hostname,
                base.port or (443 if base.scheme == "https" else 80),
            )
            probe_origin = (
                probe.scheme,
                probe.hostname,
                probe.port or (443 if probe.scheme == "https" else 80),
            )
        except ValueError as exc:
            raise ApiError(422, 422, "无效端口") from exc
        if not base.hostname or base_origin != probe_origin or probe.fragment:
            raise ApiError(422, 422, "测试地址必须与系统地址同源且不含片段")


@router.get("/token-providers")
async def list_token_providers(request: Request, user: User = Depends(MANAGERS)) -> dict[str, Any]:
    providers = request.app.state.settings.business_token_providers
    return {
        "code": 0,
        "data": [{"id": "builtin", "max_token_ttl_seconds": None}]
        + [
            {"id": name, "max_token_ttl_seconds": config.max_token_ttl_seconds}
            for name, config in sorted(providers.items())
        ],
    }


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
    values = body.row_values()
    validate_provider_settings(request, values)
    row = BusinessSystem(**values)
    session.add(row)
    await persist(session)
    await audit(
        session,
        request,
        user,
        "system.create",
        "system",
        row.key,
        {
            field: [None, getattr(row, field)]
            for field in ("token_provider", "token_delivery", "token_audience", "access_test_url")
            if getattr(row, field) is not None
        },
    )
    await persist(session, commit=True)
    catalog = await catalog_after_save(request, session, user, row)
    return {"code": 0, "data": {**system_out(row), "catalog": catalog}}


async def catalog_after_save(
    request: Request, session: AsyncSession, user: User, row: BusinessSystem
) -> dict[str, Any] | None:
    """保存后立即拉取一次目录。拉取失败不影响保存，结果写在目录状态里返回给管理台。"""
    if not row.openapi_url or not row.base_url:
        return None
    try:
        return await refresh_as(request, session, user, row)
    except ApiError as exc:
        return {"status": "skipped", "error": exc.message}


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
    values = body.row_values()
    catalog_source = (row.base_url, row.openapi_url)
    provider_fields = ("token_provider", "token_delivery", "token_audience", "access_test_url")
    for field in provider_fields:
        if field not in body.model_fields_set:
            values[field] = getattr(row, field)
    validate_provider_settings(request, values, row.token_provider)
    provider_diff = diff_dict(
        {field: getattr(row, field) for field in provider_fields},
        {field: values[field] for field in provider_fields},
    )
    for field, value in values.items():
        setattr(row, field, value)
    # 白名单变窄时实际删除授权，之后放开也不会悄悄恢复历史授权。
    removed: list[uuid.UUID] = []
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
    # 回收的授权记在审计日志里（原先写入的 system_grant_audit 表没有读者，已弃用）。
    diff = {"granted_bot_ids": [sorted(str(b) for b in removed), []]} if removed else None
    diff = {**(diff or {}), **provider_diff} or None
    await audit(session, request, user, "system.update", "system", key, diff)
    await persist(session, commit=True)
    set_etag(response, row.version)
    catalog = (
        await catalog_after_save(request, session, user, row)
        if (row.base_url, row.openapi_url) != catalog_source
        else None
    )
    return {"code": 0, "data": {**system_out(row), "catalog": catalog}}


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
    rows = (
        await session.execute(
            select(BotSystemGrant.system_key, BotSystemGrant.allow_write)
            .where(BotSystemGrant.bot_id == bot_id)
            .order_by(BotSystemGrant.system_key)
        )
    ).all()
    keys = [key for key, _ in rows]
    writes = [key for key, allowed in rows if allowed]
    return {"code": 0, "data": {"system_keys": keys, "write_keys": writes, "version": bot.version}}


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
    if body.write_keys is not None and not set(body.write_keys) <= set(keys):
        raise ApiError(422, 422, "允许写入的系统必须在授权列表中")
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
    previous = (
        await session.execute(
            delete(BotSystemGrant)
            .where(BotSystemGrant.bot_id == bot_id)
            .returning(BotSystemGrant.system_key, BotSystemGrant.allow_write)
        )
    ).all()
    before = [key for key, _ in previous]
    writes_before = sorted(key for key, allowed in previous if allowed)
    writes = sorted(
        set(body.write_keys) if body.write_keys is not None else set(writes_before) & set(keys)
    )
    session.add_all(
        [
            BotSystemGrant(
                bot_id=bot_id, system_key=key, granted_by=user.id, allow_write=key in writes
            )
            for key in keys
        ]
    )
    bot.version += 1
    await persist(session)
    await notify_bot_changed(session, bot.id)
    # 申请说明原先只落在已弃用的 system_grant_audit 表，现与授权前后对比一起进审计日志。
    diff = diff_dict(
        {"system_keys": sorted(before), "write_keys": writes_before},
        {"system_keys": keys, "write_keys": writes, "comment": body.comment or None},
    )
    await audit(session, request, user, "bot.system_grants", "bot", str(bot_id), diff)
    await persist(session, commit=True)
    return {"code": 0, "data": {"system_keys": keys, "write_keys": writes, "version": bot.version}}


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
    request: Request, user: User = Depends(ADMINS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (await session.execute(select(JwtKey).order_by(JwtKey.created_at.desc()))).scalars()
    data: list[dict[str, Any]] = [
        {
            "kid": r.kid,
            "is_active": r.is_active,
            "created_at": r.created_at,
            "retired_at": r.retired_at,
            "external": False,
        }
        for r in rows
    ]
    external = request.app.state.settings.external_jwt_key
    if external is not None:
        # 部署配置的外部签发方密钥：业务系统令牌实际由它签发，轮换平台密钥不影响它。
        data.insert(
            0,
            {
                "kid": external.kid,
                "is_active": True,
                "created_at": None,
                "retired_at": None,
                "external": True,
            },
        )
    return {"code": 0, "data": data}


@router.post("/jwt-keys/rotate")
async def rotate_key(
    request: Request, user: User = Depends(ADMINS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    key = await active_key(session, request.app.state.cipher, rotate=True)
    await audit(session, request, user, "jwt_key.rotate", "jwt_key", key.kid)
    await persist(session, commit=True)
    return {"code": 0, "data": {"kid": key.kid, "public_jwk": key.public_jwk}}


@public_router.get("/api/.well-known/jwks.json")
async def jwks(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=60"
    keys = await public_keys(session)
    # 业务系统令牌由外部签发方密钥签发时，信任本端点的系统也要能验。
    external = request.app.state.settings.external_jwt_key
    if external is not None:
        keys.append(external.public_jwk)
    return {"keys": keys}
