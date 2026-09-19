"""Owner-scoped personal MCP and interactive management of the caller's Feishu grants."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import current_user, get_session, via_bot_token
from coreman.api.errors import ApiError
from coreman.api.security import verify_csrf
from coreman.core import personal_schedules as schedules
from coreman.core.db.models import Bot, ChatSession, FeishuPersonalGrant, User
from coreman.core.feishu_personal import policy, service, tools

router = APIRouter(tags=["feishu-personal"])
# Mail bodies, sheet rows and document paragraphs arrive as tool arguments.
MAX_BODY = 262_144


@router.post("/api/runtime/feishu-personal/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """One owner's personal tools for one task: a private chat turn or an owner-only job.

    Feishu tools are further fenced by the grant generation the capability was issued for;
    a revoked, reselected or re-bound grant makes them report `authorization_changed` while
    the owner's schedule tools keep working for the rest of the turn.
    """
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid personal capability")
    cipher = request.app.state.cipher
    try:
        capability = policy.read_capability(cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid personal capability") from None
    try:
        scope = await policy.capability_scope(session, capability.task_id, capability.actor)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Verified private owner task required") from None
    if scope.scheduled != (capability.session_id is None):
        raise ApiError(403, 403, "Verified private owner task required")
    if not scope.scheduled:
        # Session then grant is also the worker opening lock order. Hold both through
        # dispatch/commit: a reset cannot acknowledge while an older operation still runs.
        base = await session.get(
            ChatSession, (scope.bot.id, scope.task.session_key), with_for_update=True
        )
        if base is None or base.relay_session_id != capability.session_id:
            raise ApiError(403, 403, "Private conversation changed")
    try:
        grant = await service.existing_row(session, cipher, scope)
        # Revalidate task cancellation after waiting for competing transactions.
        await policy.capability_scope(session, capability.task_id, capability.actor)
    except (ValueError, service.PersonalError):
        raise ApiError(403, 403, "Private authorization changed") from None
    current = (grant.context_epoch if grant else policy.NO_GRANT_EPOCH) == capability.epoch
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-feishu-personal")
    result: dict[str, Any]
    if method in ("ping", "tools/list"):
        if params:
            return mcp_rpc.error(rid, -32602, "Invalid params")
        live = grant if current else None
        level: str | None = None
        granted: set[str] = set()
        if live is not None and live.status == "connected" and live.token_enc:
            level = live.authorization_level
            granted = set(live.requested_scopes or []) & set(live.scopes or [])
        elif live is not None and live.status == "pending" and not scope.scheduled:
            # List what was asked for, so the tools are there once the owner confirms.
            level, granted = live.authorization_level, set(live.requested_scopes or [])
        definitions = tools.definitions(
            auth=current and not scope.scheduled, level=level, scopes=granted
        )
        if not scope.scheduled:
            definitions += schedules.definitions()
        result = {} if method == "ping" else {"tools": definitions}
    elif method == "tools/call":
        if (
            set(params) - {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            return mcp_rpc.error(rid, -32602, "Invalid params")
        name, arguments = params["name"], params.get("arguments", {})
        try:
            if name in schedules.TOOLS:
                value = await schedules.dispatch(session, scope, name, arguments)
            elif not current:
                value = {"error": "authorization_changed"}
            else:
                value = await tools.dispatch(session, cipher, scope, name, arguments)
        except service.PersonalError as exc:
            value = exc.payload()
        except Exception:
            # Do not let an upstream exception enter the global traceback logger: it
            # can contain OAuth response bodies, authorization URLs or credentials.
            await session.rollback()
            raise ApiError(502, 502, "Personal data service unavailable") from None
        # Identity mismatch and expired/revoked authorization clear persisted secrets.
        await session.commit()
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get("/api/runtime/feishu-personal/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})


async def interactive_user(request: Request, actor: User = Depends(current_user)) -> User:
    login = getattr(request.state, "admin_session", None)
    if via_bot_token(request) or login is None or login.user_id != actor.id:
        raise ApiError(403, 403, "Interactive login required")
    return actor


@router.get("/api/me/feishu-authorizations")
async def authorizations(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(FeishuPersonalGrant, Bot.name)
            .join(Bot, Bot.id == FeishuPersonalGrant.bot_id)
            .where(FeishuPersonalGrant.user_id == actor.id)
            .order_by(Bot.name, Bot.id)
        )
    ).all()
    response.headers.update(mcp_rpc.NO_STORE)
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    **service._state(grant, request.app.state.cipher),
                    "bot_id": str(grant.bot_id),
                    "bot_name": name,
                    "status": (
                        "expired"
                        if grant.status in ("pending", "selecting")
                        and grant.pending_expires_at
                        and grant.pending_expires_at <= datetime.now(UTC)
                        else grant.status
                    ),
                    "scopes": list(grant.scopes or []),
                    "authorization_level": grant.authorization_level,
                    "requested_scopes": list(grant.requested_scopes or []),
                    "missing_scopes": sorted(
                        set(grant.requested_scopes or []) - set(grant.scopes or [])
                    )
                    if grant.status == "connected"
                    else [],
                    "expires_at": (
                        (
                            grant.pending_expires_at
                            if grant.status in ("pending", "selecting")
                            else grant.expires_at
                        ).isoformat()
                        if (
                            grant.pending_expires_at
                            if grant.status in ("pending", "selecting")
                            else grant.expires_at
                        )
                        else None
                    ),
                }
                for grant, name in rows
            ]
        },
    }


@router.delete("/api/me/feishu-authorizations/{bot_id}", dependencies=[Depends(verify_csrf)])
async def revoke(
    bot_id: uuid.UUID,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await service.revoke_grant(session, bot_id, actor.id, request.app.state.cipher)
    await session.commit()
    response.headers.update(mcp_rpc.NO_STORE)
    return {"code": 0, "data": {"ok": True, "remote_revoked": result["remote_revoked"]}}
