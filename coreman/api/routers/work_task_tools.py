"""AI 员工的任务工具 MCP 端点：一轮一张能力令牌，只能以这一轮的执行员工身份领取。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, Task, WorkTask
from coreman.core.work_tasks import agent_tools, tools

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
    # 任务会话（work:<编号>）里、这件事此刻在这位员工手上：另有做任务用的工具。
    current = None
    session_key = task.session_key or ""
    if session_key.startswith("work:"):
        current = await session.scalar(
            select(WorkTask)
            .where(WorkTask.code == session_key.removeprefix("work:"))
            .with_for_update()
        )
        if current is not None and current.assignee_bot_id != bot.id:
            current = None
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
        listed = tools.definitions() + (agent_tools.definitions() if current else [])
        result = {} if method == "ping" else {"tools": listed}
    elif method == "tools/call":
        if (
            set(params) - {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            return mcp_rpc.error(rid, -32602, "Invalid params")
        name, arguments = params["name"], params.get("arguments", {})
        if name in agent_tools.NAMES:
            value = (
                await agent_tools.invoke(
                    session,
                    current,
                    bot,
                    name,
                    arguments,
                    chat_task_id=task.id,
                    base_url=request.app.state.settings.public_base_url,
                )
                if current is not None
                else {"error": "not_in_task", "stop": True, "message": "这一轮不在做任务"}
            )
        else:
            value = await tools.invoke(session, capability, bot, name, arguments)
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
