"""Private task MCP and interactive management of the caller's Feishu grants."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError
from coreman.api.security import verify_csrf
from coreman.core.db.models import Bot, ChatSession, FeishuPersonalGrant, User
from coreman.core.feishu_personal import policy, service, tools

router = APIRouter(tags=["feishu-personal"])
NO_STORE = {"Cache-Control": "no-store"}


def _response(body: dict[str, Any]) -> Response:
    return Response(
        json.dumps(body, ensure_ascii=False), media_type="application/json", headers=NO_STORE
    )


def _error(rid: Any, code: int, message: str) -> Response:
    return _response({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


@router.post("/api/runtime/feishu-personal/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid personal capability")
    try:
        task_id, actor, epoch, base_session_id = policy.read_capability(
            request.app.state.cipher, auth[7:]
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid personal capability") from None
    try:
        scope = await policy.task_scope(session, task_id, actor)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Verified private human task required") from None
    # Session then grant is also the worker opening/mode-switch lock order. Hold
    # both through dispatch/commit: reset or mode exit cannot acknowledge while
    # an older personal operation is still using its capability.
    base = await session.get(
        ChatSession, (scope.bot.id, scope.task.session_key), with_for_update=True
    )
    if base is None or base.relay_session_id != base_session_id:
        raise ApiError(403, 403, "Private conversation changed")
    try:
        grant, _ = await service._row(session, request.app.state.cipher, scope)
        # Revalidate task cancellation after waiting for competing transactions.
        await policy.task_scope(session, task_id, actor)
    except (ValueError, service.PersonalError):
        raise ApiError(403, 403, "Private authorization changed") from None
    if grant.context_epoch != epoch or grant.assistant_mode != "personal":
        raise ApiError(403, 403, "Private authorization changed")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16384:
            raise ApiError(413, 413, "Request too large")
    try:
        body = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _error(None, -32700, "Parse error")
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or set(body) - {"jsonrpc", "id", "method", "params"}
        or not isinstance(body.get("method"), str)
        or ("id" in body and type(body["id"]) not in (str, int, type(None)))
    ):
        return _error(None, -32600, "Invalid request")
    rid, method, params = body.get("id"), body["method"], body.get("params", {})
    if not isinstance(params, dict):
        return _error(rid, -32602, "Invalid params")
    # MCP request metadata is transport context, never tool arguments or identity.
    if "_meta" in params:
        if not isinstance(params["_meta"], dict):
            return _error(rid, -32602, "Invalid params")
        params = {key: item for key, item in params.items() if key != "_meta"}
    if "id" not in body:
        # Notifications never run tools or mutate a grant.
        if method != "notifications/initialized" or params:
            return _error(None, -32600, "Invalid notification")
        return Response(status_code=202, headers=NO_STORE)
    result: dict[str, Any]
    if method == "initialize":
        if set(params) - {"protocolVersion", "capabilities", "clientInfo"}:
            return _error(rid, -32602, "Invalid params")
        requested = params.get("protocolVersion")
        version = (
            requested if requested in ("2025-03-26", "2025-06-18", "2025-11-25") else "2025-03-26"
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coreman-feishu-personal", "version": "1.0"},
        }
    elif method in ("ping", "tools/list"):
        if params:
            return _error(rid, -32602, "Invalid params")
        allow_send = bool(
            grant
            and grant.status == "connected"
            and grant.authorization_level == "all"
            and {"im:message", "im:message.send_as_user"}.issubset(
                set(grant.scopes or []) & set(grant.requested_scopes or [])
            )
        )
        result = {} if method == "ping" else {"tools": tools.definitions(allow_send=allow_send)}
    elif method == "tools/call":
        if (
            set(params) - {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            return _error(rid, -32602, "Invalid params")
        try:
            value = await tools.dispatch(
                session,
                request.app.state.cipher,
                scope,
                params["name"],
                params.get("arguments", {}),
            )
        except service.PersonalError as exc:
            value = {"error": exc.code}
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
        return _error(rid, -32601, "Method not found")
    return _response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get("/api/runtime/feishu-personal/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **NO_STORE})


async def interactive_user(request: Request, actor: User = Depends(current_user)) -> User:
    login = getattr(request.state, "admin_session", None)
    if request.cookies.get("bot_token") or login is None or login.user_id != actor.id:
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
    response.headers.update(NO_STORE)
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
    response.headers.update(NO_STORE)
    return {"code": 0, "data": {"ok": True, "remote_revoked": result["remote_revoked"]}}
