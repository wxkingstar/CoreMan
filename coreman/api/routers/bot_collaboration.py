"""Task-scoped stateless MCP and backwards-compatible help transport."""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.chat import bot_collaboration as service
from coreman.core.chat import collaboration_tools as tools

router = APIRouter(tags=["bot-collaboration"])
MAX_BODY = 16384


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
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY), request.headers)
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    try:
        await tools.task_scope(session, task_id, actor)
    except ValueError as exc:
        raise ApiError(403, 403, str(exc)) from None
    hello = mcp_rpc.hello(rid, method, params, "coreman-collaboration")
    if hello is not None:
        return hello
    result: dict[str, Any]
    if method == "ping":
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
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get("/api/runtime/collaboration/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
