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
from coreman.core.db.models import WORK_TASK_OPEN, Bot, Task, WorkTask
from coreman.core.work_tasks import agent_tools, tools

router = APIRouter(tags=["work-tasks"])
MAX_BODY = 65_536


async def _current(session: AsyncSession, task: Task, bot: Bot, *, lock: bool) -> WorkTask | None:
    session_key = task.session_key or ""
    if not session_key.startswith("work:"):
        return None
    stmt = select(WorkTask).where(WorkTask.code == session_key.removeprefix("work:"))
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None or row.assignee_bot_id != bot.id or row.status not in WORK_TASK_OPEN:
        return None
    return row


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
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY), request.headers)
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    # 任务会话（work:<编号>）里、这件事此刻在这位员工手上而且没结束：另有做任务用的工具。
    # 只有调用这些工具时才锁任务行，列工具、领取别的任务都不锁。
    locking = method == "tools/call" and params.get("name") in agent_tools.NAMES
    current = await _current(session, task, bot, lock=locking)
    hello = mcp_rpc.hello(rid, method, params, "coreman-tasks")
    if hello is not None:
        return hello
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
        value: dict[str, Any] | str
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
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": not isinstance(value, str) and "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get(tools.API_PATH)
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})
