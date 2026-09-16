"""Task-scoped stateless MCP and backwards-compatible help transport."""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.chat import bot_collaboration as service
from coreman.core.chat import collaboration_tools as tools

router = APIRouter(tags=["bot-collaboration"])


class HelpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_bot_key: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)


def capability(request: Request) -> tuple[int, str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "求助凭证无效")
    try:
        return service.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError):
        raise ApiError(401, 401, "求助凭证无效或已过期") from None


@router.post("/api/runtime/bot-help")
async def request_help(
    body: HelpIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    task_id, actor = capability(request)
    try:
        result = await tools.invoke(
            session,
            cipher=request.app.state.cipher,
            task_id=task_id,
            actor=actor,
            name="request_collaboration",
            arguments={"collaborator_id": body.target_bot_key, "question": body.question.strip()},
        )
        # Commit failure meters/cancellation too. Raising before commit would reset the guard.
        await session.commit()
    except ValueError as exc:
        raise ApiError(409, 409, str(exc)) from None
    if "error" in result:
        raise ApiError(409, 409, result["error"])
    return {**result, "instruction": result["message"]}


@router.post("/api/runtime/collaboration/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    task_id, actor = capability(request)
    # This endpoint is for native CLI clients, never browser-originated requests.
    if request.headers.get("Origin"):
        raise ApiError(403, 403, "Browser origins are not supported")
    raw = await request.body()
    if len(raw) > 16384:
        raise ApiError(413, 413, "Request too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return rpc_error(None, -32700, "Parse error")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid request")
    rid, method = body.get("id"), body.get("method")
    if not isinstance(method, str) or isinstance(rid, (dict, list, bool)):
        return rpc_error(None, -32600, "Invalid request")
    try:
        await tools.task_scope(session, task_id, actor)
    except ValueError as exc:
        raise ApiError(403, 403, str(exc)) from None
    if "id" not in body:
        return Response(status_code=202)
    params = body.get("params", {})
    if not isinstance(params, dict):
        return rpc_error(rid, -32602, "Invalid params")
    result: dict[str, Any]
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (
            requested if requested in ("2025-03-26", "2025-06-18", "2025-11-25") else "2025-03-26"
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coreman-collaboration", "version": "1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools.tool_definitions()}
    elif method == "tools/call":
        name, arguments = params.get("name", ""), params.get("arguments", {})
        # Malformed tool calls count as failed calls as well.
        if not isinstance(name, str) or not isinstance(arguments, dict):
            name, arguments = "invalid", {}
        try:
            value = await tools.invoke(
                session,
                task_id=task_id,
                actor=actor,
                name=name,
                arguments=arguments,
                cipher=request.app.state.cipher,
            )
            await session.commit()
        except ValueError as exc:
            value = {"error": str(exc), "stop": True}
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return rpc_error(rid, -32601, "Method not found")
    return Response(
        json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False),
        media_type="application/json",
    )


def rpc_error(rid: Any, code: int, message: str) -> Response:
    return Response(
        json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}),
        media_type="application/json",
    )


@router.get("/api/runtime/collaboration/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
