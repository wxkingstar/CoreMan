"""群文档 MCP：飞书群里真人发起的一轮，按链接读本群看得到、或提问人放行的文档。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.db.models import ChatSession
from coreman.core.feishu_docs import policy, tools
from coreman.core.feishu_personal import files

router = APIRouter(tags=["feishu-docs"])
MAX_BODY = 262_144


@router.post("/api/runtime/feishu-docs/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid docs capability")
    cipher = request.app.state.cipher
    try:
        capability = policy.read_capability(cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid docs capability") from None
    try:
        scope = await policy.scope(session, capability)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Verified group task required") from None
    # 会话被重置（/new）之后，旧凭证不能接着用。
    base = await session.get(ChatSession, (scope.bot.id, scope.task.session_key))
    if base is None or base.relay_session_id != capability.session_id:
        raise ApiError(403, 403, "Group conversation changed")
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-feishu-docs")
    result: dict[str, Any]
    if method in ("ping", "tools/list"):
        if params:
            return mcp_rpc.error(rid, -32602, "Invalid params")
        result = {} if method == "ping" else {"tools": tools.definitions()}
    elif method == "tools/call":
        if (
            set(params) - {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            return mcp_rpc.error(rid, -32602, "Invalid params")
        try:
            value = await tools.dispatch(
                session,
                cipher,
                scope,
                params["name"],
                params.get("arguments", {}),
                files.FileRelay(request.app.state.settings, cipher),
            )
        except Exception:
            # 上游异常可能带着令牌或文档内容，不进全局日志。
            await session.rollback()
            raise ApiError(502, 502, "Docs service unavailable") from None
        await session.commit()
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get("/api/runtime/feishu-docs/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
