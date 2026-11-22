"""个人凭证接口：agent 用本轮令牌发起索取；本人登录后在网页上提交、查看、更新、删除。

任何响应、错误与审计都不带值；提交值只在 service.submit() 里过一次手就加密落库
（一次性交付暂存在请求上，续接轮结束即擦掉）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    CredentialBinding,
    CredentialRequest,
    PersonalCredential,
    User,
)
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
            "save": opened.save,
            "message": service.agent_note(cap, opened.save),
        },
    }


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(max_length=policy.MAX_FIELDS)
    # {变量名: 本人已有凭证的 ID}：这一项不填值，直接用这条凭证。
    use: dict[str, uuid.UUID] = Field(default_factory=dict, max_length=policy.MAX_FIELDS)


class BotsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_ids: list[uuid.UUID] = Field(max_length=500)


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
    options, defaults = (
        await store.choices(session, bot_id=row.bot_id, user_id=actor.id, fields=row.fields)
        if row.save and status == "open"
        else ({}, {})
    )
    return {
        "code": 0,
        "data": {
            "id": str(row.id),
            "bot_name": name,
            "platform": bot.platform if bot else "",
            "purpose": row.purpose,
            "fields": row.fields,
            "save": row.save,
            "status": status,
            "expires_at": row.expires_at.isoformat(),
            "security_note": cards.security_note(name, save=row.save),
            "choices": {
                key: [
                    {
                        "id": str(c.id),
                        "label": c.label,
                        "env_key": c.env_key,
                        "bots": list(c.bots),
                    }
                    for c in items
                ]
                for key, items in options.items()
            },
            "defaults": {key: str(value) for key, value in defaults.items()},
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
            session,
            request.app.state.cipher,
            request_id,
            actor_id=actor.id,
            values=body.values,
            picks=body.use,
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


def _out(
    cipher: Cipher, binding: CredentialBinding, row: PersonalCredential, bot: Bot
) -> dict[str, Any]:
    return {
        "credential_id": str(row.id),
        "bot_id": str(bot.id),
        "bot_name": bot.name,
        "env_key": binding.env_key,
        "label": row.label,
        "secret": row.secret,
        "value": store.plain_value(cipher, row),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": binding.last_used_at.isoformat() if binding.last_used_at else None,
    }


def _item(cipher: Cipher, owned: store.Owned) -> dict[str, Any]:
    row = owned.credential
    used = [b.last_used_at for b, _ in owned.bindings if b.last_used_at]
    return {
        "id": str(row.id),
        "env_key": row.env_key,
        "label": row.label,
        "secret": row.secret,
        "value": store.plain_value(cipher, row),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": max(used).isoformat() if used else None,
        "bindings": [
            {
                "bot_id": str(bot.id),
                "bot_name": bot.name,
                "env_key": binding.env_key,
                "last_used_at": (
                    binding.last_used_at.isoformat() if binding.last_used_at else None
                ),
            }
            for binding, bot in owned.bindings
        ],
    }


@router.get("/api/me/credentials")
async def list_credentials(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按（AI 员工, 变量名）逐行列出；共用的凭证在每个 AI 员工下各出现一次。"""
    response.headers.update(mcp_rpc.NO_STORE)
    rows = await store.list_own(session, actor.id)
    cipher = request.app.state.cipher
    return {"code": 0, "data": [_out(cipher, b, row, bot) for b, row, bot in rows]}


async def _audit(
    session: AsyncSession,
    actor: User,
    action: str,
    bot_id: uuid.UUID | None,
    key: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot" if bot_id else "user",
        target_id=str(bot_id or actor.id),
        diff={"keys": [key], **(extra or {})},
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
    """更新这个 AI 员工这个变量名用的凭证；共用它的 AI 员工一起生效。"""
    response.headers.update(mcp_rpc.NO_STORE)
    value = _clean(env_key, body.value)
    cipher = request.app.state.cipher
    found = await store.bound_credential(session, bot_id=bot_id, user_id=actor.id, env_key=env_key)
    row = (
        await store.update_value(
            session, cipher, credential_id=found.id, user_id=actor.id, value=value
        )
        if found
        else None
    )
    if row is None:
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.updated", bot_id, env_key)
    await session.commit()
    rows = await store.list_own(session, actor.id)
    binding, _, bot = next(
        (b, r, bot) for b, r, bot in rows if b.bot_id == bot_id and b.env_key == env_key
    )
    return {"code": 0, "data": _out(cipher, binding, row, bot)}


@router.delete("/api/me/credentials/{bot_id}/{env_key}", dependencies=[Depends(verify_csrf)])
async def delete_credential(
    bot_id: uuid.UUID,
    env_key: str,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """这个 AI 员工不再用这个变量名；凭证没有别的 AI 员工在用就一并删除。"""
    response.headers.update(mcp_rpc.NO_STORE)
    if not await store.unbind(session, bot_id=bot_id, user_id=actor.id, env_key=env_key):
        raise not_found("凭证不存在")
    await _audit(session, actor, "personal_credential.deleted", bot_id, env_key)
    await session.commit()
    return {"code": 0, "data": None}


def _clean(env_key: str, value: str) -> str:
    try:
        return policy.clean_values([{"key": env_key, "label": env_key}], {env_key: value})[env_key]
    except CredentialError as exc:
        raise _error(exc) from None


@router.get("/api/me/credential-items")
async def list_items(
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按凭证列出，每条带上它绑给了哪些 AI 员工。"""
    response.headers.update(mcp_rpc.NO_STORE)
    cipher = request.app.state.cipher
    return {
        "code": 0,
        "data": [_item(cipher, owned) for owned in await store.list_items(session, actor.id)],
    }


@router.get("/api/me/credential-bots")
async def bindable_bots(
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bots = await service.bindable_bots(session, actor.id)
    return {
        "code": 0,
        "data": [{"id": str(b.id), "name": b.name, "platform": b.platform} for b in bots],
    }


async def _owned_item(
    request: Request, session: AsyncSession, actor: User, credential_id: uuid.UUID
) -> dict[str, Any]:
    owned = next(
        (o for o in await store.list_items(session, actor.id) if o.credential.id == credential_id),
        None,
    )
    if owned is None:
        raise not_found("凭证不存在")
    return _item(request.app.state.cipher, owned)


@router.put("/api/me/credential-items/{credential_id}", dependencies=[Depends(verify_csrf)])
async def update_item(
    credential_id: uuid.UUID,
    body: ValueIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    row = await session.get(PersonalCredential, credential_id)
    if row is None or row.user_id != actor.id:
        raise not_found("凭证不存在")
    value = _clean(row.env_key, body.value)
    await store.update_value(
        session, request.app.state.cipher, credential_id=row.id, user_id=actor.id, value=value
    )
    await _audit(session, actor, "personal_credential.updated", None, row.env_key)
    await session.commit()
    return {"code": 0, "data": await _owned_item(request, session, actor, credential_id)}


@router.put("/api/me/credential-items/{credential_id}/bots", dependencies=[Depends(verify_csrf)])
async def set_item_bots(
    credential_id: uuid.UUID,
    body: BotsIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把凭证绑定的 AI 员工改成正好这些；新加的只能是本人可用的 AI 员工。"""
    response.headers.update(mcp_rpc.NO_STORE)
    row = await session.get(PersonalCredential, credential_id)
    if row is None or row.user_id != actor.id:
        raise not_found("凭证不存在")
    wanted = set(body.bot_ids)
    have = set(
        await session.scalars(
            select(CredentialBinding.bot_id).where(CredentialBinding.credential_id == row.id)
        )
    )
    allowed = {b.id for b in await service.bindable_bots(session, actor.id)}
    if wanted - have - allowed:
        raise ApiError(403, 403, "只能绑定你可以使用的 AI 员工")
    added, removed = await store.set_bots(session, row, wanted)
    if added or removed:
        await _audit(
            session,
            actor,
            "personal_credential.bound",
            None,
            row.env_key,
            {
                "credential_id": str(row.id),
                "added": [str(b) for b in added],
                "removed": [str(b) for b in removed],
            },
        )
    await session.commit()
    return {"code": 0, "data": await _owned_item(request, session, actor, credential_id)}


@router.delete("/api/me/credential-items/{credential_id}", dependencies=[Depends(verify_csrf)])
async def delete_item(
    credential_id: uuid.UUID,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除整条凭证：所有 AI 员工都不再注入它。"""
    response.headers.update(mcp_rpc.NO_STORE)
    row = await session.get(PersonalCredential, credential_id)
    if row is None or row.user_id != actor.id:
        raise not_found("凭证不存在")
    key = row.env_key
    if not await store.delete(session, credential_id=credential_id, user_id=actor.id):
        raise not_found("凭证不存在")
    await _audit(
        session, actor, "personal_credential.deleted", None, key, {"credential_id": str(row.id)}
    )
    await session.commit()
    return {"code": 0, "data": None}
