"""Business system operation catalog: the task-scoped MCP for AI employees, and admin refresh."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.routers.infra_system_test import SUBJECT_UNAVAILABLE_MESSAGES
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.auth.system_access import SubjectUnavailable, granted_systems, token_subject
from coreman.core.db.models import BusinessSystem, SystemCatalog, Task, User
from coreman.core.systems_catalog import policy, service, tools

router = APIRouter(tags=["systems-catalog"])
admin_router = APIRouter(
    prefix="/api/admin", tags=["systems-catalog"], dependencies=[Depends(verify_csrf)]
)
MANAGERS = require_roles("ai_committee", "platform_admin")
MAX_BODY = 16384
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
    state = dict(task.payload.get(tools.BUDGET_KEY) or {})
    stop = tools.charge(state, name, arguments)
    task.payload = {**task.payload, tools.BUDGET_KEY: state}
    await session.commit()
    if stop is not None:
        return stop
    try:
        subject = await token_subject(session, scope.user)
    except SubjectUnavailable as exc:
        return {"error": "subject_unavailable", "reason": exc.reason}
    ctx = tools.Context(
        session=session,
        issuance=await issuance(request),
        operator=service.Operator(
            user=scope.user, subject=subject, task_id=task.id, bot_id=scope.bot.id
        ),
        systems=await granted_systems(session, scope.bot.id),
        perms=dict(state.get("perms") or {}),
    )
    value = await tools.dispatch(ctx, name, arguments)
    if ctx.perms_changed:
        task = await session.get(Task, task.id, with_for_update=True, populate_existing=True)
        assert task is not None
        current = dict(task.payload.get(tools.BUDGET_KEY) or {})
        current["perms"] = {**(current.get("perms") or {}), **ctx.perms}
        task.payload = {**task.payload, tools.BUDGET_KEY: current}
    await session.commit()
    return value


@router.post(policy.API_PATH)
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """One task's catalog tools, as the member the task runs for.

    The capability is checked on every request and the task, AI employee and member are reloaded,
    so a stopped task, a disabled employee or a revoked grant takes effect on the next call.
    """
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
        result = {} if method == "ping" else {"tools": tools.tool_definitions()}
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


@router.get(policy.API_PATH)
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
