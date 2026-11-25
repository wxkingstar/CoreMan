"""AI 员工的任务工具 MCP 端点：一轮一张能力令牌，只能以这一轮的执行员工身份领取。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, Task
from coreman.core.work_tasks import tools

router = APIRouter(tags=["work-tasks"])
MAX_BODY = 65_536


@router.post(tools.API_PATH)
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid task capability")
    try:
        capability = tools.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid task capability") from None
    # 这一轮还在跑、属于这位员工，员工还启用着：令牌才算数。
    task = await session.get(Task, capability.task_id)
    bot = await session.get(Bot, capability.bot_id)
    if (
        task is None
        or task.bot_id != capability.bot_id
        or task.status not in tasks.ACTIVE
        or bot is None
        or not bot.enabled
    ):
        raise ApiError(403, 403, "Task turn is no longer active")
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-tasks")
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
        value = await tools.invoke(
            session, capability, bot, params["name"], params.get("arguments", {})
        )
        await session.commit()
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get(tools.API_PATH)
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
