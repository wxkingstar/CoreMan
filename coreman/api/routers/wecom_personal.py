"""本人专属的企业微信 MCP，以及「我的企业微信」里对本人连接的查看与断开。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.db.models import Bot, ChatSession, User, WecomPersonalGrant
from coreman.core.wecom_personal import policy, service, tools

router = APIRouter(tags=["wecom-personal"])
# 覆盖整篇文档、写长邮件都要把正文放进参数里，比飞书个人工具的上限宽。
MAX_BODY = 262_144


@router.post("/api/runtime/wecom-personal/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """一位本人、一个任务的企业微信工具：本人私聊的一轮，或本人专属的定时任务。

    工具还受签发时授权版本的约束：断开、重新选档位或授权人变了，本轮剩下的调用都报
    `authorization_changed`。
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
        # 先锁会话再锁授权，与 worker 开场的顺序一致；两把锁都持有到提交。
        base = await session.get(
            ChatSession, (scope.bot.id, scope.task.session_key), with_for_update=True
        )
        if base is None or base.relay_session_id != capability.session_id:
            raise ApiError(403, 403, "Private conversation changed")
    grant = await service.existing_row(session, scope.bot.id, scope.user_id)
    try:
        # 等锁期间任务可能被取消：拿到锁之后再核一次来源。
        await policy.capability_scope(session, capability.task_id, capability.actor)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Private authorization changed") from None
    current = (
        grant is not None
        and grant.status == "connected"
        and grant.context_epoch == capability.epoch
    )
    parsed = mcp_rpc.parse(await mcp_rpc.read_body(request, MAX_BODY))
    if isinstance(parsed, Response):
        return parsed
    rid, method, params = parsed
    if method == "initialize":
        return mcp_rpc.initialize(rid, params, "coreman-wecom-personal")
    result: dict[str, Any]
    if method in ("ping", "tools/list"):
        if params:
            return mcp_rpc.error(rid, -32602, "Invalid params")
        definitions = tools.definitions(grant.authorization_level) if current and grant else []
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
            if not current or grant is None:
                value = {"error": "authorization_changed"}
            else:
                value = await tools.dispatch(session, cipher, scope, grant, name, arguments)
        except service.PersonalError as exc:
            value = {"error": exc.code}
            if exc.code in ("authorization_changed", "authorization_required"):
                value["hint"] = "请本人在私聊里重新发送“连接企业微信”。"
        except Exception:
            # 上游异常不进全局日志：里面可能带着令牌或企业微信返回的正文。
            await session.rollback()
            raise ApiError(502, 502, "WeCom personal service unavailable") from None
        # 授权人变了会清掉令牌，换了令牌也要落库。
        await session.commit()
        result = {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "isError": "error" in value,
        }
    else:
        return mcp_rpc.error(rid, -32601, "Method not found")
    return mcp_rpc.response({"jsonrpc": "2.0", "id": rid, "result": result})


@router.get("/api/runtime/wecom-personal/mcp")
async def no_sse() -> Response:
    return Response(status_code=405, headers={"Allow": "POST", **mcp_rpc.NO_STORE})


@router.get("/api/me/wecom-authorizations")
async def authorizations(
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(WecomPersonalGrant, Bot.name)
            .join(Bot, Bot.id == WecomPersonalGrant.bot_id)
            .where(WecomPersonalGrant.user_id == actor.id)
            .order_by(Bot.name, Bot.id)
        )
    ).all()
    response.headers.update(mcp_rpc.NO_STORE)
    return {
        "code": 0,
        "data": {
            "items": [
                {**service.state(grant), "bot_id": str(grant.bot_id), "bot_name": name}
                for grant, name in rows
            ]
        },
    }


@router.delete("/api/me/wecom-authorizations/{bot_id}", dependencies=[Depends(verify_csrf)])
async def revoke(
    bot_id: uuid.UUID,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await service.revoke_grant(session, bot_id, actor.id)
    await session.commit()
    response.headers.update(mcp_rpc.NO_STORE)
    return {"code": 0, "data": {"ok": True}}
