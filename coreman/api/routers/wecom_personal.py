"""本人专属的企业微信 MCP，以及「我的企业微信」里的扫码绑定、档位与能力状态。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.chat.openuserid import OpenUseridResolver
from coreman.core.db.models import ChatSession, User, UserIdentity, WecomPersonalBinding
from coreman.core.wecom_personal import binding, policy, service, tools

router = APIRouter(tags=["wecom-personal"])
# 覆盖整篇文档、写长邮件都要把正文放进参数里，比飞书个人工具的上限宽。
MAX_BODY = 262_144
REBIND_HINT = "请本人在 CoreMan「我的企业微信」页面重新扫码绑定。"
FAILURE_HINTS = {
    "authorization_changed": REBIND_HINT,
    "authorization_required": REBIND_HINT,
    "credentials_rejected": "本人的授权机器人已被删除或重置了 Secret。" + REBIND_HINT,
}


@router.post("/api/runtime/wecom-personal/mcp")
async def mcp(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """一位本人、一个任务的企业微信工具：本人私聊的一轮，或本人专属的定时任务。

    工具用本人自己的授权机器人凭证，还受签发时授权版本的约束：暂停、解除、重新绑定或换档位，
    本轮剩下的调用都报 `authorization_changed`。
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
    row = await service.load(session, scope.user_id)
    try:
        # 等锁期间任务可能被取消：拿到锁之后再核一次来源。
        await policy.capability_scope(session, capability.task_id, capability.actor)
    except (ValueError, KeyError, TypeError):
        raise ApiError(403, 403, "Private authorization changed") from None
    current = service.usable(row) and row is not None and row.context_epoch == capability.epoch
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
        definitions = tools.definitions(row.authorization_level) if current and row else []
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
            if not current or row is None:
                value = {"error": "authorization_changed"}
            else:
                value = await tools.dispatch(cipher, row, name, arguments)
        except service.PersonalError as exc:
            value = {"error": exc.code}
            if exc.code in FAILURE_HINTS:
                value["hint"] = FAILURE_HINTS[exc.code]
        except Exception:
            # 上游异常不进全局日志：里面可能带着令牌或企业微信返回的正文。
            await session.rollback()
            raise ApiError(502, 502, "WeCom personal service unavailable") from None
        # 能力状态、换来的令牌、失效时清掉的凭证都要落库。
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


class BindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_level: Literal["readonly", "all_except_send", "all"] | None = None
    enabled: bool | None = None


def _resolver(request: Request) -> OpenUseridResolver:
    """企业微信回传的授权人常是密文 userid：用自建应用换成明文，再对到 CoreMan 用户。"""
    state = request.app.state
    resolver: OpenUseridResolver | None = getattr(state, "wecom_openuserid", None)
    if resolver is None:
        resolver = OpenUseridResolver(state.session_factory, state.cipher)
        state.wecom_openuserid = resolver
    return resolver


async def _linked(session: AsyncSession, user_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.user_id == user_id, UserIdentity.platform == "wecom"
        )
    )
    return found is not None


async def _out(
    request: Request,
    session: AsyncSession,
    response: Response,
    user: User,
    row: WecomPersonalBinding | None,
    retry_after: int | None = None,
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    return {
        "code": 0,
        "data": {
            **service.state(row),
            "scan": binding.scan_state(request.app.state.cipher, row, retry_after),
            "identity_linked": await _linked(session, user.id),
            "auth_ttl_days": service.AUTH_TTL.days,
        },
    }


def _personal_failure(exc: service.PersonalError) -> ApiError:
    if exc.code == "upstream_unavailable":
        return ApiError(502, 502, "暂时连不上企业微信，请稍后重试")
    if exc.code == "invalid_level":
        return ApiError(422, 422, "不支持的档位")
    return ApiError(409, 409, "还没有绑定企业微信，或绑定已失效，请重新扫码绑定")


@router.get("/api/me/wecom-binding")
async def get_binding(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(WecomPersonalBinding, actor.id)
    return await _out(request, session, response, actor, row)


@router.post("/api/me/wecom-binding/scan", dependencies=[Depends(verify_csrf)])
async def start_scan(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """生成绑定二维码。扫码人必须就是本人，所以要先有本人的企业微信账号才能核对。"""
    if not await _linked(session, actor.id):
        raise ApiError(
            422, 422, "你的企业微信账号还没有同步到 CoreMan，暂时无法核对扫码人，请联系管理员"
        )
    row = await service.load(session, actor.id, create=True)
    assert row is not None
    try:
        await binding.start(request.app.state.cipher, row)
    except service.PersonalError:
        raise ApiError(429, 429, "操作太频繁，请稍后再试") from None
    await session.commit()
    return await _out(request, session, response, actor, row, binding.POLL_SECONDS)


@router.get("/api/me/wecom-binding/scan")
async def poll_scan(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await service.load(session, actor.id)
    retry = None
    if row is not None:
        retry = await binding.refresh(session, request.app.state.cipher, row, _resolver(request))
        await session.commit()
    return await _out(request, session, response, actor, row, retry)


@router.delete("/api/me/wecom-binding/scan", dependencies=[Depends(verify_csrf)])
async def cancel_scan(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await service.load(session, actor.id)
    if row is not None and binding.pending(row):
        # 刚扫完码就关窗口：先查一次，已经建好的授权机器人不能白白丢掉。
        if row.scan_next_poll_at is not None:
            row.scan_next_poll_at = None
        await binding.refresh(session, request.app.state.cipher, row, _resolver(request))
        binding.cancel(row)
        await session.commit()
    return await _out(request, session, response, actor, row)


@router.patch("/api/me/wecom-binding", dependencies=[Depends(verify_csrf)])
async def update_binding(
    body: BindingUpdate,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await service.load(session, actor.id)
    if row is None:
        raise _personal_failure(service.PersonalError("authorization_required"))
    try:
        if body.authorization_level is not None:
            service.set_level(row, body.authorization_level)
        if body.enabled is not None:
            service.set_enabled(row, body.enabled)
    except service.PersonalError as exc:
        raise _personal_failure(exc) from None
    await session.commit()
    return await _out(request, session, response, actor, row)


async def _probe(request: Request, session: AsyncSession, actor: User, renewed: bool) -> Any:
    row = await service.load(session, actor.id)
    if row is None or row.status != "bound":
        raise _personal_failure(service.PersonalError("authorization_required"))
    try:
        await service.probe(request.app.state.cipher, row)
    except service.PersonalError as exc:
        # 凭证被拒会把绑定作废：先落库，再如实返回状态。
        await session.commit()
        if exc.code != "credentials_rejected":
            raise _personal_failure(exc) from None
        return row
    if renewed:
        service.mark_renewed(row)
    await session.commit()
    return row


@router.post("/api/me/wecom-binding/check", dependencies=[Depends(verify_csrf)])
async def check_binding(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """逐项试一次只读调用，刷新各项能力的状态。"""
    row = await _probe(request, session, actor, renewed=False)
    return await _out(request, session, response, actor, row)


@router.post("/api/me/wecom-binding/renewed", dependencies=[Depends(verify_csrf)])
async def renewed_binding(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """本人已在企业微信里续期：重新检查，并让能用的各项从现在起算 7 天。"""
    row = await _probe(request, session, actor, renewed=True)
    return await _out(request, session, response, actor, row)


@router.delete("/api/me/wecom-binding", dependencies=[Depends(verify_csrf)])
async def delete_binding(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除托管的凭证并作废签出去的能力凭据；企业微信里的授权机器人由本人自行删除。"""
    row = await service.load(session, actor.id)
    if row is not None:
        binding.cancel(row)
        service.unbind(row)
        await session.commit()
    return await _out(request, session, response, actor, row)
