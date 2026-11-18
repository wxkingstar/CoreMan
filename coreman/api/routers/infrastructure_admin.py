"""系统授权与基础设施凭证管理；所有密钥只在创建或轮换时返回一次。"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import delete, or_, select, update
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
from coreman.api.system_grants import apply_grants, current_grants, lock_bot
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import diff_dict, record_audit
from coreman.core.auth.system_access import RESERVED_SYSTEM_KEYS, granted_systems
from coreman.core.auth.tokens import active_key, public_keys
from coreman.core.bots.permissions import is_bot_admin
from coreman.core.db.models import (
    ApiClient,
    Bot,
    BotSkill,
    BotSystemGrant,
    BusinessSystem,
    JwtKey,
    RelayServer,
    RuntimeNode,
    Skill,
    SystemCatalog,
    User,
)
from coreman.core.db.models.infrastructure import PROXY_LIMITS
from coreman.core.knowledge import installation as installs
from coreman.core.relay.models import backend_of
from coreman.core.systems_catalog import policy as catalog_policy
from coreman.core.systems_catalog import service as catalog_service
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
    # env：令牌下发到运行环境；proxy：只经平台代理调用（systems_call），令牌不进运行环境；
    # mcp：系统是一个 MCP 服务（mcp_url），运行时每轮带着发言者令牌挂载。
    token_delivery: Literal["env", "proxy", "mcp"] = "env"
    mcp_url: str | None = Field(default=None, max_length=2048)
    # 授权给机器人时自动安装的技能；要能不经审批、不填参数直接装上。
    companion_skill_id: uuid.UUID | None = None
    token_audience: str | None = Field(default=None, max_length=2048)
    access_test_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    sort_order: int = 0
    default_for_all_bots: bool = False
    # 空列表 = 显式白名单且暂无机器人；null = 对全部机器人开放，必须由管理员明确选择。
    # 缺省按空白名单处理：bot 管理员能控制提示词与 env，而平台会把发言者令牌注入其 CLI，
    # 新系统不能在没人确认的情况下对所有机器人开放。
    allowed_bot_ids: list[uuid.UUID] | None = Field(default_factory=list, max_length=500)
    # 平台代理的批量与调用上限，范围见 PROXY_LIMITS；更新时不传沿用库里的值。
    batch_write_max: int = Field(default=PROXY_LIMITS["batch_write_max"][0])
    batch_destructive_max: int = Field(default=PROXY_LIMITS["batch_destructive_max"][0])
    proxy_calls_per_turn: int = Field(default=PROXY_LIMITS["proxy_calls_per_turn"][0])
    batch_interval_ms: int = Field(default=PROXY_LIMITS["batch_interval_ms"][0])

    @field_validator(*PROXY_LIMITS)
    @classmethod
    def within_limits(cls, value: int, info: ValidationInfo) -> int:
        _default, low, high = PROXY_LIMITS[str(info.field_name)]
        if not low <= value <= high:
            raise ValueError(f"须在 {low} 到 {high} 之间")
        return value

    @field_validator(
        "token_audience", "access_test_url", "openapi_url", "sitemap_url", "mcp_url", mode="before"
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

    @field_validator("mcp_url")
    @classmethod
    def valid_mcp_url(cls, value: str | None) -> str | None:
        if value:
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.fragment
                or any(c.isspace() for c in value)
            ):
                raise ValueError("MCP 地址须为不带凭证和片段的 HTTPS 地址")
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
            "mcp_url",
            "companion_skill_id",
            "token_provider",
            "token_audience",
            "access_test_url",
            "enabled",
            "sort_order",
            "default_for_all_bots",
            "allowed_bot_ids",
            *PROXY_LIMITS,
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


async def validate_delivery(session: AsyncSession, values: dict[str, Any]) -> None:
    if values["token_delivery"] == "mcp":
        if not values.get("mcp_url"):
            raise ApiError(422, 422, "MCP 系统必须填写 MCP 地址")
        if values.get("openapi_url"):
            raise ApiError(422, 422, "MCP 系统不使用 OpenAPI 操作目录")
    elif values.get("mcp_url"):
        raise ApiError(422, 422, "只有 MCP 交付的系统可以填写 MCP 地址")
    skill_id = values.get("companion_skill_id")
    if skill_id:
        # 默认授权所有机器人的系统没有授权动作，配套技能无从自动安装。
        if values.get("default_for_all_bots"):
            raise ApiError(422, 422, "默认授权所有机器人的系统不能设置配套技能")
        problem = installs.companion_problem(await session.get(Skill, skill_id))
        if problem:
            raise ApiError(422, 422, problem)


async def validate_proxy(
    session: AsyncSession, values: dict[str, Any], row: BusinessSystem | None
) -> None:
    """切到平台代理前确认调得通：代理系统没有可用的操作目录时，AI 员工只会被告知「无法调用」。

    只拦这次保存造成的坏状态（`row` 是保存前的系统，新建时为 None）；改回 env / mcp 不设门槛。
    """
    if values["token_delivery"] != "proxy":
        return
    if not values.get("openapi_url"):
        # 保存前就是没有 OpenAPI 地址的代理系统：照常让它改别的字段。
        if row is not None and row.token_delivery == "proxy" and not row.openapi_url:
            return
        raise ApiError(
            422,
            422,
            "平台代理的系统必须填写 OpenAPI 地址：AI 员工只能按操作目录经平台调用，"
            "没有目录就完全调用不了。请先填写 OpenAPI 地址、确认操作目录拉取成功后再改为平台代理；"
            "还没有 OpenAPI 描述的系统请保留原来的交付方式",
        )
    if row is None or row.token_delivery == "proxy":
        return
    # 这次同时改了地址的，目录要保存后才重新拉取，此刻的状态说明不了什么。
    if (row.base_url, row.openapi_url) != (values.get("base_url"), values["openapi_url"]):
        return
    catalog = await session.get(SystemCatalog, row.key)
    if (
        catalog is not None
        and catalog.status == "error"
        and catalog.error != catalog_service.PENDING
    ):
        raise ApiError(
            409,
            409,
            f"操作目录当前不可用（{catalog.error}），改为平台代理后 AI 员工将无法调用这个系统。"
            "请先让业务系统修好 OpenAPI 描述，在系统页刷新操作目录"
            f"（或 coreman systems refresh {row.key}），目录可用后再切换",
        )


async def delivery_rows(
    session: AsyncSession, user: User, mode: str | None
) -> list[dict[str, Any]]:
    """系统 × 能用它的 AI 员工（只看已启用的员工）。

    授权口径直接用每轮签发的那一个（`granted_systems`），盘点结果才和实际下发一致。
    别人的个人助理不列：助理记录只对所有者可见，管理员也不例外。
    """
    bots = list(
        await session.scalars(
            select(Bot)
            .where(Bot.enabled, or_(Bot.kind == "standard", Bot.owner_id == user.id))
            .order_by(Bot.bot_key)
        )
    )
    relays = {
        relay.id: relay
        for relay in await session.scalars(
            select(RelayServer).where(RelayServer.id.in_([bot.relay_server_id for bot in bots]))
        )
    }
    nodes = {
        node.id: node
        for node in await session.scalars(
            select(RuntimeNode).where(
                RuntimeNode.id.in_([relay.runtime_node_id for relay in relays.values()])
            )
        )
    }
    writes = {
        (bot_id, key): allowed
        for bot_id, key, allowed in await session.execute(
            select(BotSystemGrant.bot_id, BotSystemGrant.system_key, BotSystemGrant.allow_write)
        )
    }
    found: list[tuple[BusinessSystem, dict[str, Any]]] = []
    for bot in bots:
        relay = relays.get(bot.relay_server_id) if bot.relay_server_id else None
        node = nodes.get(relay.runtime_node_id) if relay and relay.runtime_node_id else None
        # 与 `catalog_policy.mount_for` 同一判断：节点在用，且该员工的后端声明了目录能力。
        catalog_runtime = (
            relay is not None
            and node is not None
            and node.is_active
            and catalog_policy.runtime_supported(
                node.capabilities, backend_of(bot.model, relay.model_provider)
            )
        )
        for system in await granted_systems(session, bot.id):
            if mode and system.token_delivery != mode:
                continue
            allow_write = writes.get((bot.id, system.key))
            found.append(
                (
                    system,
                    {
                        "system_key": system.key,
                        "system_name": system.name,
                        "token_delivery": system.token_delivery,
                        "bot_id": str(bot.id),
                        "bot_key": bot.bot_key,
                        "bot_name": bot.name,
                        # grant：员工的系统权限里显式授权；default：系统默认对全部员工开放。
                        "source": "default" if allow_write is None else "grant",
                        "allow_write": bool(allow_write),
                        "runtime_node": node.name if node else None,
                        "catalog_runtime": catalog_runtime,
                        "unprompted_system_tokens": bot.unprompted_system_tokens,
                    },
                )
            )
    found.sort(key=lambda pair: (pair[0].sort_order, pair[0].key, pair[1]["bot_key"]))
    return [row for _, row in found]


def delivery_audit(values: dict[str, Any]) -> dict[str, Any]:
    skill_id = values.get("companion_skill_id")
    return {
        "mcp_url": values.get("mcp_url"),
        "companion_skill_id": str(skill_id) if skill_id else None,
    }


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
    await validate_delivery(session, values)
    await validate_proxy(session, values, None)
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
            field: [None, value]
            for field, value in (
                {
                    field: getattr(row, field)
                    for field in (
                        "token_provider",
                        "token_delivery",
                        "token_audience",
                        "access_test_url",
                    )
                }
                | delivery_audit(values)
            ).items()
            if value is not None
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
    # 旧管理台不认识 MCP 两项和代理上限，不传时沿用库里的值。
    for field in (*provider_fields, "mcp_url", "companion_skill_id", *PROXY_LIMITS):
        if field not in body.model_fields_set:
            values[field] = getattr(row, field)
    validate_provider_settings(request, values, row.token_provider)
    await validate_delivery(session, values)
    await validate_proxy(session, values, row)
    provider_diff = diff_dict(
        {field: getattr(row, field) for field in (*provider_fields, *PROXY_LIMITS)}
        | delivery_audit({"mcp_url": row.mcp_url, "companion_skill_id": row.companion_skill_id}),
        {field: values[field] for field in (*provider_fields, *PROXY_LIMITS)}
        | delivery_audit(values),
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
    # 已随系统装上的技能留给机器人，改按手动安装处理。
    await session.execute(
        update(BotSkill).where(BotSkill.installed_by_system == key).values(installed_by_system=None)
    )
    await audit(session, request, user, "system.delete", "system", key)
    await persist(session, commit=True)
    return {"code": 0, "data": None}


@router.get("/system-delivery")
async def list_delivery(
    mode: Literal["env", "proxy", "mcp"] | None = None,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """盘点各系统的令牌交付方式和用得到它的 AI 员工：切换交付方式前看影响面。"""
    return {"code": 0, "data": await delivery_rows(session, user, mode)}


@router.get("/bots/{bot_id}/system-grants")
async def get_grants(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not is_bot_admin(user, bot, await member_ids_of(session, bot_id)):
        raise forbidden()
    keys, writes = await current_grants(session, bot_id)
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
    # 先锁机器人行并读回最新版本号再核对 If-Match：读出之后别处改过的，这里能看出来。
    await lock_bot(session, bot)
    require_if_match(request, bot.version)
    keys, writes = await apply_grants(
        session,
        request,
        user,
        bot,
        keys=body.system_keys,
        write_keys=body.write_keys,
        comment=body.comment,
    )
    await persist(session, commit=True)
    return {"code": 0, "data": {"system_keys": keys, "write_keys": writes, "version": bot.version}}


async def sync_companions(
    session: AsyncSession,
    request: Request,
    user: User,
    bot: Bot,
    granted: list[BusinessSystem],
    before: set[str],
    removed: set[str],
) -> tuple[list[str], list[str]]:
    """新授权的系统装上配套技能，撤销的系统卸掉随它装上的技能。返回两边的技能 id。

    仍被其他已授权系统用作配套的技能不卸载。
    """
    installed: list[str] = []
    for system in granted:
        if system.key in before or system.companion_skill_id is None:
            continue
        skill = await session.get(Skill, system.companion_skill_id)
        if skill is None:
            continue
        if await installs.install_companion(
            session,
            request.app.state.cipher,
            bot=bot,
            skill=skill,
            actor=user,
            system_key=system.key,
        ):
            installed.append(str(skill.id))
    uninstalled: list[str] = []
    if removed:
        rows = list(
            await session.scalars(
                select(BotSkill).where(
                    BotSkill.bot_id == bot.id,
                    BotSkill.installed_by_system.in_(removed),
                    BotSkill.status != "uninstalled",
                )
            )
        )
        still_needed = {s.companion_skill_id for s in granted}
        for row in rows:
            if row.skill_id in still_needed:
                continue
            # 同时是全局技能的，只去掉系统来源，留给全局技能管。
            if row.installed_globally:
                row.installed_by_system = None
                continue
            await installs.uninstall(session, bot, row, user)
            uninstalled.append(str(row.skill_id))
    return installed, uninstalled


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
