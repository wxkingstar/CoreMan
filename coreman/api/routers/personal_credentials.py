"""个人凭证接口：agent 用本轮令牌发起索取；本人登录后在网页上提交、查看、更新、删除。

任何响应、错误与审计都不带值；提交值只在 service.submit() 里过一次手就加密落库。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, CredentialRequest, PersonalCredential, User
from coreman.core.personal_credentials import cards, policy, service, store
from coreman.core.personal_credentials.policy import CredentialError
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["personal-credentials"])
MAX_BODY = 65_536
_STATUS = {
    "invalid_fields": 422,
    "invalid_values": 422,
    "unreachable": 409,
    "login_unavailable": 409,
    "inactive": 403,
    "forbidden": 403,
    "not_found": 404,
}


def _error(exc: CredentialError) -> ApiError:
    status = _STATUS.get(exc.code, 400)
    return ApiError(status, status, exc.message, [{"type": exc.code}])


@router.post("/api/runtime/credentials/requests")
async def create_request(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if "origin" in request.headers:
        raise ApiError(403, 403, "Browser origins are not supported")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "Invalid credential capability")
    try:
        cap = policy.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ApiError(401, 401, "Invalid credential capability") from None
    raw = await mcp_rpc.read_body(request, MAX_BODY)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError(422, 422, "请求体必须是 JSON") from None
    try:
        opened = await service.open_request(
            session,
            request.app.state.cipher,
            cap,
            body,
            base_url=request.app.state.settings.public_base_url,
        )
    except CredentialError as exc:
        await session.rollback()
        raise _error(exc) from None
    await session.commit()
    response.status_code = 202 if opened.status == "form_sent" else 200
    return {
        "code": 0,
        "data": {
            "status": opened.status,
            "request_id": str(opened.request_id),
            "message": service.AGENT_NOTE_CRON if cap.origin_kind == "cron" else service.AGENT_NOTE,
        },
    }


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(max_length=policy.MAX_FIELDS)


class ValueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=policy.MAX_VALUE * 2)


@router.get("/api/me/credential-requests/{request_id}")
async def get_request(
    request_id: uuid.UUID,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    row = await session.get(CredentialRequest, request_id)
    if row is None:
        raise not_found("表单不存在")
    if row.user_id != actor.id:
        raise ApiError(403, 403, "只有发起人本人可以打开这张表单")
    bot = await session.get(Bot, row.bot_id)
    name = bot.name if bot else "AI 员工"
    status = "expired" if row.status == "open" and row.expires_at <= utcnow() else row.status
    return {
        "code": 0,
        "data": {
            "id": str(row.id),
            "bot_name": name,
            "platform": bot.platform if bot else "",
            "purpose": row.purpose,
            "fields": row.fields,
            "status": status,
            "expires_at": row.expires_at.isoformat(),
            "security_note": cards.security_note(name),
        },
    }


@router.post("/api/me/credential-requests/{request_id}/submit", dependencies=[Depends(verify_csrf)])
async def submit_request(
    request_id: uuid.UUID,
    body: SubmitIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    try:
        result = await service.submit(
            session, request.app.state.cipher, request_id, actor_id=actor.id, values=body.values
        )
    except CredentialError as exc:
        await session.rollback()
        raise _error(exc) from None
    # 过期也要提交：submit() 已把状态改成 expired。
    await session.commit()
    if result.status == "duplicate":
        raise ApiError(409, 409, result.message)
    if result.status == "expired":
        raise ApiError(410, 410, result.message)
    if result.status == "invalid":
        raise ApiError(422, 422, result.message)
    return {
        "code": 0,
        "data": {"status": "saved", "keys": list(result.keys), "message": result.message},
    }


def _out(cipher: Cipher, row: PersonalCredential, bot: Bot) -> dict[str, Any]:
    return {
        "bot_id": str(bot.id),
        "bot_name": bot.name,
        "env_key": row.env_key,
        "label": row.label,
        "secret": row.secret,
        "value": store.plain_value(cipher, row),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@router.get("/api/me/credentials")
async def list_credentials(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    rows = await store.list_own(session, actor.id)
    return {"code": 0, "data": [_out(request.app.state.cipher, row, bot) for row, bot in rows]}


async def _audit(
    session: AsyncSession, actor: User, action: str, bot_id: uuid.UUID, key: str
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot_id),
        diff={"keys": [key]},
    )


@router.put("/api/me/credentials/{bot_id}/{env_key}", dependencies=[Depends(verify_csrf)])
async def update_credential(
    bot_id: uuid.UUID,
    env_key: str,
    body: ValueIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    try:
        value = policy.clean_values([{"key": env_key, "label": env_key}], {env_key: body.value})[
            env_key
        ]
    except CredentialError as exc:
        raise _error(exc) from None
    cipher = request.app.state.cipher
    row = await store.update_value(
        session, cipher, bot_id=bot_id, user_id=actor.id, env_key=env_key, value=value
    )
    if row is None:
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.updated", bot_id, env_key)
    await session.commit()
    bot = await session.get(Bot, bot_id)
    assert bot is not None
    return {"code": 0, "data": _out(cipher, row, bot)}


@router.delete("/api/me/credentials/{bot_id}/{env_key}", dependencies=[Depends(verify_csrf)])
async def delete_credential(
    bot_id: uuid.UUID,
    env_key: str,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    if not await store.delete(session, bot_id=bot_id, user_id=actor.id, env_key=env_key):
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.deleted", bot_id, env_key)
    await session.commit()
    return {"code": 0, "data": None}
