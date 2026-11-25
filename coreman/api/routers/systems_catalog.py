"""Business system operation catalog: the task-scoped MCP for AI employees, the catalog MCP for
members' own agents (and anonymous readers), and admin refresh."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.routers.infra_system_test import SUBJECT_UNAVAILABLE_MESSAGES
from coreman.api.routers.objects import serve
from coreman.api.security import verify_csrf
from coreman.core.assistants import kinds
from coreman.core.assistants.approvals import proxied, tower
from coreman.core.audit import record_audit
from coreman.core.auth.system_access import (
    SubjectUnavailable,
    catalog_systems,
    granted_systems,
    token_subject,
)
from coreman.core.db.models import (
    BotSystemGrant,
    BusinessSystem,
    StoredObject,
    SystemCall,
    SystemCatalog,
    Task,
    User,
)
from coreman.core.db.models.oauth import OAuthGrant
from coreman.core.oauth import server as oauth
from coreman.core.systems_catalog import (
    context,
    member_writes,
    policy,
    service,
    spill,
    tools,
    uploads,
)
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["systems-catalog"])
admin_router = APIRouter(
    prefix="/api/admin", tags=["systems-catalog"], dependencies=[Depends(verify_csrf)]
)
MANAGERS = require_roles("ai_committee", "platform_admin")
# Proxied writes carry their request body in the tool arguments.
MAX_BODY = 262_144
PUBLIC_PATH = "/api/systems/mcp"
# Catalog tools take a few short arguments; the anonymous endpoint accepts nothing larger.
PUBLIC_MAX_BODY = 16_384
# `?token_env=` names the caller's own variable for the curl templates; a name, never a value.
TOKEN_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
LINT_SHOWN = 20


async def issuance(request: Request) -> service.Issuance:
    state = request.app.state
    return service.Issuance(
        cipher=state.cipher,
        issuer=str(await state.settings_store.get("jwt_issuer", default="coreman")),
        external_key=state.settings.external_jwt_key,
        providers=state.settings.business_token_providers,
    )


def catalog_out(row: SystemCatalog | None) -> dict[str, Any] | None:
    if row is None:
        return None
    lint = row.lint or []
    return {
        "status": row.status,
        "error": row.error,
        "spec_url": row.spec_url,
        "spec_bytes": row.spec_bytes,
        "fetched_at": row.fetched_at,
        "checked_at": row.checked_at,
        "module_count": row.module_count,
        "operation_count": row.operation_count,
        "hidden_count": row.hidden_count,
        "lint_errors": sum(1 for item in lint if item.get("severity") == "error"),
        "lint_warnings": sum(1 for item in lint if item.get("severity") == "warn"),
        "lint": lint[:LINT_SHOWN],
    }


async def refresh_as(
    request: Request, session: AsyncSession, user: User, system: BusinessSystem
) -> dict[str, Any] | None:
    """Fetch the catalog now as this administrator; None when the system has no OpenAPI URL."""
    if not system.openapi_url:
        return None
    try:
        subject = await token_subject(session, user)
    except SubjectUnavailable as exc:
        # 操作者在业务系统里没有身份就不拉取：拿别人的身份去读描述不可接受。
        raise ApiError(422, 422, SUBJECT_UNAVAILABLE_MESSAGES[exc.reason]) from exc
    row = await service.refresh(
        session,
        await issuance(request),
        system,
        service.Operator(user=user, subject=subject),
        force=True,
    )
    return catalog_out(row)


@admin_router.get("/systems/{key}/catalog")
async def get_catalog(
    key: str, user: User = Depends(MANAGERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if await session.get(BusinessSystem, key) is None:
        raise not_found("系统不存在")
    return {"code": 0, "data": catalog_out(await session.get(SystemCatalog, key))}


@admin_router.post("/systems/{key}/catalog/refresh")
async def refresh_catalog(
    key: str,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    system = await session.get(BusinessSystem, key)
    if system is None:
        raise not_found("系统不存在")
    if not system.openapi_url or not system.base_url:
        raise ApiError(422, 422, "请先填写系统地址和 OpenAPI 地址")
    if user.source == "bootstrap" or not user.login_name:
        raise ApiError(403, 403, "请使用已绑定的真实用户刷新目录")
    await record_audit(
        session,
        action="system.catalog_refresh",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="system",
        target_id=key,
        ip=client_ip(request),
    )
    return {"code": 0, "data": await refresh_as(request, session, user, system)}


async def call_tool(
    request: Request,
    session: AsyncSession,
    capability: policy.Capability,
    scope: policy.Scope,
    name: str,
    arguments: Any,
) -> dict[str, Any]:
    # Count the call first and commit it: business-system requests below never hold the task lock.
    task = await session.get(Task, capability.task_id, with_for_update=True, populate_existing=True)
    assert task is not None
    state = dict(task.payload.get(context.BUDGET_KEY) or {})
    key = tools.proxied_system(name, arguments)
    limit = (
        None
        if key is None
        else await session.scalar(
            select(BusinessSystem.proxy_calls_per_turn).where(BusinessSystem.key == key)
        )
    )
    stop = tools.charge(state, name, arguments, proxy_limit=limit or tools.DEFAULT_PROXY_CALLS)
    task.payload = {**task.payload, context.BUDGET_KEY: state}
    await session.commit()
    if stop is not None:
        return stop
    try:
        subject = await token_subject(session, scope.user)
    except SubjectUnavailable as exc:
        return {"error": "subject_unavailable", "reason": exc.reason}
    writable = frozenset(
        await session.scalars(
            select(BotSystemGrant.system_key).where(
                BotSystemGrant.bot_id == scope.bot.id, BotSystemGrant.allow_write
            )
        )
    )
    # 个人助理无人在场、又能提交审批的一轮：授权允许写的系统，写操作改为提交所有者审批。
    stage = proxied.may_stage(task, scope.bot, scope.user)
    ctx = context.Context(
        session=session,
        issuance=await issuance(request),
        operator=service.Operator(
            user=scope.user, subject=subject, task_id=task.id, bot_id=scope.bot.id
        ),
        systems=await granted_systems(session, scope.bot.id),
        writable=policy.writable_for(task, writable),
        writes_preapproved=policy.writes_preapproved(task),
        owner_absent=kinds.unattended(task.kind),
        stageable=writable if stage else frozenset(),
        approval_cfg=tower.config(request.app.state.settings) if stage else None,
        token_ttl=scope.bot.sse_timeout_seconds,
        perms=dict(state.get("perms") or {}),
        tokens=dict(state.get("tokens") or {}),
        settings=request.app.state.settings,
    )
    value = await tools.dispatch(ctx, name, arguments)
    if ctx.perms_changed or ctx.tokens_changed:
        task = await session.get(Task, task.id, with_for_update=True, populate_existing=True)
        assert task is not None
        current = dict(task.payload.get(context.BUDGET_KEY) or {})
        current["perms"] = {**(current.get("perms") or {}), **ctx.perms}
        current["tokens"] = {**(current.get("tokens") or {}), **ctx.tokens}
        task.payload = {**task.payload, context.BUDGET_KEY: current}
    await session.commit()
    return value


@router.post(policy.API_PATH)
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """One task's catalog tools, as the member the task runs for.

    The capability is checked on every request and the task, AI employee and member are reloaded,
    so a stopped task, a disabled employee or a revoked grant takes effect on the next call.
    """
    capability, scope = await _capability_scope(request, session)
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-systems")
    result: dict[str, Any]
    if method in ("ping", "tools/list"):
        if params:
            return mcp_rpc.error(rid, -32602, "Invalid params")
        if method == "ping":
            result = {}
        else:
            systems = await granted_systems(session, scope.bot.id)
            proxied = any(system.token_delivery == "proxy" for system in systems)
            result = {"tools": tools.tool_definitions(proxied=proxied)}
    elif method == "tools/call":
        if (
            set(params) - {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            return mcp_rpc.error(rid, -32602, "Invalid params")
        value = await call_tool(
            request, session, capability, scope, params["name"], params.get("arguments", {})
        )
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


async def _capability_scope(
    request: Request, session: AsyncSession
) -> tuple[policy.Capability, policy.Scope]:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid catalog capability")
    try:
        capability = policy.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid catalog capability") from None
    try:
        scope = await policy.task_scope(session, capability)
    except ValueError:
        raise ApiError(403, 403, "Active task required") from None
    return capability, scope


@router.get(spill.OBJECT_PATH + "{identity}")
async def stored_result(
    identity: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """A large proxied result of this turn, for its runtime only: the same capability as the
    catalog MCP, and only objects a call of this task by this member stored."""
    capability, _ = await _capability_scope(request, session)
    owned = await session.scalar(
        select(SystemCall.id).where(
            SystemCall.object_id == identity,
            SystemCall.task_id == capability.task_id,
            SystemCall.user_id == capability.actor,
        )
    )
    row = await session.get(StoredObject, identity)
    if owned is None or row is None or row.expires_at <= utcnow():
        raise not_found("结果不存在或已过期")
    return await serve(request.app.state.settings, row)


@router.put(uploads.UPLOAD_PATH)
async def upload_file(request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    """A file for an upload or import operation of a proxied system, from this turn's runtime:
    the same capability as the catalog MCP. The returned upload ID goes into `systems_call`."""
    capability, scope = await _capability_scope(request, session)
    filename = (request.query_params.get("filename") or "").strip()
    if not filename or len(filename) > 200:
        raise ApiError(400, 400, "请用 ?filename= 给出文件名（不超过 200 个字符）")
    limit = uploads.UPLOAD_LIMIT
    too_large = f"文件超过 {limit // (1024 * 1024)} MB"
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, 413, too_large)

    async def body() -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise ApiError(413, 413, too_large)
            yield chunk

    row = await uploads.save(
        session,
        request.app.state.settings,
        body(),
        filename=filename,
        mime=uploads.content_type(filename, request.headers.get("content-type")),
        task_id=capability.task_id,
        user_id=scope.user.id,
    )
    if row.size == 0:
        raise ApiError(400, 400, "文件为空")
    await session.commit()
    return {
        "upload_id": str(row.id),
        **uploads.describe(row),
        "expires_in": int(uploads.TTL.total_seconds()),
    }


@router.get(policy.API_PATH)
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})


def _challenge(request: Request, error: str | None = None) -> Response:
    """401/403 带 RFC 9728 的资源元数据地址：支持 MCP 授权规范的客户端据此自动走登录流程。"""
    base = str(request.app.state.settings.public_base_url).rstrip("/")
    value = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource{PUBLIC_PATH}"'
    if error:
        value += f', error="{error}"'
    if error == "insufficient_scope":
        value += ', scope="mcp"'
    status = 403 if error == "insufficient_scope" else 401
    return Response(
        json.dumps({"error": error or "unauthorized"}),
        status_code=status,
        media_type="application/json",
        headers={"WWW-Authenticate": value, **mcp_rpc.NO_STORE},
    )


class _RateLimit:
    """按成员限制目录调用次数（每个 API 进程各自计数，够挡住失控的循环）。"""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[uuid.UUID, list[float]] = {}

    def allow(self, key: uuid.UUID) -> bool:
        now = time.monotonic()
        recent = [t for t in self.hits.get(key, []) if now - t < 60]
        if len(recent) >= self.per_minute:
            self.hits[key] = recent
            return False
        recent.append(now)
        self.hits[key] = recent
        if len(self.hits) > 10_000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
        return True


MEMBER_CALLS_PER_MINUTE = 120
_member_limit = _RateLimit(MEMBER_CALLS_PER_MINUTE)


@router.post(PUBLIC_PATH)
async def public_mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """The catalog for callers outside AI employees' tasks: browse, search and describe.

    With `Authorization: Bearer` (a CoreMan OAuth access token with the `mcp` scope) the caller is
    a member's own agent, rate limited per member: lists are filtered by the member's permissions,
    and platform-proxied systems are called as the member through `systems_call` (writes wait for
    the member's click on a Feishu card, see `member_writes`). The member's CoreMan token never
    reaches a business system: the platform issues the member a business token of its own.
    Without a token the catalog is anonymous, read-only and unfiltered, and only when the
    deployment sets SYSTEMS_CATALOG_PUBLIC; otherwise the 401 points the client at the
    authorization server.
    """
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    member: User | None = None
    grant: OAuthGrant | None = None
    token = oauth.bearer(request.headers.get("Authorization"))
    if token is not None:
        try:
            grant, member = await oauth.authenticate(session, token, "mcp")
        except oauth.Unauthenticated as exc:
            return _challenge(request, exc.error)
    elif "authorization" in request.headers:
        return _challenge(request, "invalid_token")
    elif not request.app.state.settings.systems_catalog_public:
        return _challenge(request)
    token_env = request.query_params.get("token_env", context.DIRECT_TOKEN_ENV)
    if not TOKEN_ENV.fullmatch(token_env):
        raise ApiError(400, 400, "token_env must be an environment variable name")
    # Members' writes carry their request body in the tool arguments.
    limit = MAX_BODY if member is not None else PUBLIC_MAX_BODY
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, limit))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-systems")
    if method not in ("ping", "tools/list", "tools/call"):
        return mcp_rpc.error(rid, -32601, "Method not found")
    if method in ("ping", "tools/list") and params:
        return mcp_rpc.error(rid, -32602, "Invalid params")
    if method == "ping":
        return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": {}})
    if method == "tools/call" and (
        set(params) - {"name", "arguments"}
        or not isinstance(params.get("name"), str)
        or not isinstance(params.get("arguments", {}), dict)
    ):
        return mcp_rpc.error(rid, -32602, "Invalid params")
    if member is not None and method == "tools/call" and not _member_limit.allow(member.id):
        return mcp_rpc.error(rid, -32000, "调用过于频繁，请稍后再试")
    ctx: context.Context | None = None
    unavailable: str | None = None
    if member is not None and grant is not None:
        try:
            ctx = await member_writes.build_context(session, await issuance(request), member, grant)
        except SubjectUnavailable as exc:
            # 业务系统认不出这个人：目录照旧给（不过滤、直连），代理调用不行。
            unavailable = exc.reason
        if ctx is not None:
            ctx.token_env = token_env
    if ctx is None:
        ctx = context.Context(
            session=session,
            issuance=await issuance(request),
            operator=None,
            systems=await catalog_systems(session),
            anonymous=True,
            token_env=token_env,
        )
    result: dict[str, Any]
    if method == "tools/list":
        proxied = ctx.own_agent and any(s.token_delivery == "proxy" for s in ctx.systems)
        result = {"tools": tools.tool_definitions(proxied=proxied, own_agent=ctx.own_agent)}
    else:
        name, arguments = params["name"], params.get("arguments", {})
        if unavailable is not None and name in ("systems_call", "systems_call_result"):
            value: dict[str, Any] = {
                "error": "subject_unavailable",
                "reason": unavailable,
                "message": "平台算不出你在业务系统里的身份（缺少邮箱或邮箱前缀与他人重复），"
                "不能代你调用。请联系管理员补全账号信息。",
            }
        else:
            value = await tools.dispatch(ctx, name, arguments)
        await session.commit()
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get(PUBLIC_PATH)
async def public_no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
