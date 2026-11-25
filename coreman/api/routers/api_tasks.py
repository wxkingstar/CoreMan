"""通过 API 驱动 AI 员工：外部程序用个人令牌发起、查询、取消；本人在管理台管理自己的令牌。

见 docs/superpowers/specs/2026-11-06-api-driven-tasks.md。令牌明文只在创建的响应里出现一次。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.infra_auth import require_scope
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.api_tasks import service, tokens
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.audit import record_audit
from coreman.core.db.models import ApiClient, ApiTask, Bot, PersonalAccessToken, User
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["api-tasks"])


def _error(exc: TokenError) -> ApiError:
    return ApiError(exc.status, exc.status, exc.message)


async def _caller(request: Request, session: AsyncSession) -> tuple[PersonalAccessToken, User]:
    if "origin" in request.headers:
        # 令牌给脚本和服务端用：浏览器里跨站带着令牌发请求一律不收。
        raise ApiError(403, 403, "不支持从浏览器页面调用")
    try:
        token, user = await tokens.authenticate(session, request.headers.get("Authorization", ""))
    except TokenError as exc:
        raise _error(exc) from None
    return token, user


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["p2p", "chat"] = "p2p"
    alias: str | None = Field(default=None, max_length=32)


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_key: str = Field(min_length=1, max_length=50)
    prompt: str = Field(min_length=1, max_length=service.PROMPT_MAX)
    request_id: str | None = Field(default=None, min_length=1, max_length=service.REQUEST_ID_MAX)
    conversation: ConversationIn = Field(default_factory=ConversationIn)


@router.post("/api/infra/tasks", status_code=202)
async def create_task(
    body: TaskIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token, user = await _caller(request, session)
    # 先把「最近使用」提交掉：认证那条 UPDATE 锁着令牌行，不能一直锁到问完飞书。
    await session.commit()
    try:
        row, created = await service.create(
            session,
            token=token,
            user=user,
            bot_key=body.bot_key,
            prompt=body.prompt,
            request_id=body.request_id,
            conversation=body.conversation.model_dump(exclude_none=True),
            cipher=request.app.state.cipher,
        )
    except TokenError as exc:
        # 认证时记下的「最近使用」照样留着：用过就是用过。
        await session.commit()
        raise _error(exc) from None
    if created:
        await record_audit(
            session,
            action="api_task.create",
            actor_id=user.id,
            actor_login=user.login_name,
            target_type="bot",
            target_id=str(row.bot_id),
            diff={
                "api_task_id": str(row.id),
                "token_id": str(token.id),
                "target": row.target,
                "alias": row.alias,
            },
            ip=client_ip(request),
        )
    data = await service.view(session, row)
    await session.commit()
    if not created:
        response.status_code = 200
    return {"code": 0, "data": data}


async def _own_task(
    session: AsyncSession, token: PersonalAccessToken, task_id: uuid.UUID
) -> ApiTask:
    """只看得到这枚令牌自己发起的任务：交给某个脚本的令牌看不到本人别的令牌发起的事。"""
    row = await session.get(ApiTask, task_id)
    if row is None or row.token_id != token.id:
        raise not_found("任务不存在")
    return row


@router.get("/api/infra/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    token, _ = await _caller(request, session)
    row = await _own_task(session, token, task_id)
    data = await service.view(session, row)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/api/infra/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    token, _ = await _caller(request, session)
    row = await _own_task(session, token, task_id)
    await service.cancel(session, row)
    await session.commit()
    return {"code": 0, "data": await service.view(session, row)}


# ---- 业务系统代员工发起（API 调用方签名，`tasks` 接口组） --------------------------------------


class ClientTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: str = Field(min_length=1, max_length=128)
    bot_key: str = Field(min_length=1, max_length=50)
    prompt: str = Field(min_length=1, max_length=service.PROMPT_MAX)
    request_id: str | None = Field(default=None, min_length=1, max_length=service.REQUEST_ID_MAX)


class ClientUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: str = Field(min_length=1, max_length=128)


tasks_client = require_scope(service.CLIENT_SCOPE)


@router.post("/api/infra/tasks/run", status_code=202)
async def create_client_task(
    body: ClientTaskIn,
    request: Request,
    response: Response,
    client: ApiClient = Depends(tasks_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        row, created = await service.create_for_client(
            session,
            client=client,
            subject=body.user,
            bot_key=body.bot_key,
            prompt=body.prompt,
            request_id=body.request_id,
        )
    except TokenError as exc:
        await session.rollback()
        raise _error(exc) from None
    if created:
        await record_audit(
            session,
            action="api_task.create",
            actor_id=row.user_id,
            actor_login=body.user.strip().lower(),
            target_type="bot",
            target_id=str(row.bot_id),
            diff={"api_task_id": str(row.id), "api_client": client.app_key},
            ip=client_ip(request),
        )
    data = await service.view(session, row)
    await session.commit()
    if not created:
        response.status_code = 200
    return {"code": 0, "data": data}


async def _client_task(
    session: AsyncSession, client: ApiClient, task_id: uuid.UUID, subject: str
) -> ApiTask:
    """只看得到这个调用方自己为这位员工发起的任务。"""
    row = await session.get(ApiTask, task_id)
    if row is None or row.client_key != client.app_key:
        raise not_found("任务不存在")
    user = await session.get(User, row.user_id)
    prefix = (user.email or "").split("@", 1)[0].lower() if user else ""
    if not prefix or prefix != subject.strip().lower():
        raise not_found("任务不存在")
    return row


@router.get("/api/infra/tasks/run/{task_id}")
async def get_client_task(
    task_id: uuid.UUID,
    user: str = Query(min_length=1, max_length=128),
    client: ApiClient = Depends(tasks_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _client_task(session, client, task_id, user)
    data = await service.view(session, row)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/api/infra/tasks/run/{task_id}/cancel")
async def cancel_client_task(
    task_id: uuid.UUID,
    body: ClientUserIn,
    client: ApiClient = Depends(tasks_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _client_task(session, client, task_id, body.user)
    await service.cancel(session, row)
    await session.commit()
    return {"code": 0, "data": await service.view(session, row)}


# ---- 本人的令牌 ------------------------------------------------------------------------------


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=tokens.NAME_MAX)
    bot_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    days: int = Field(ge=1, le=tokens.MAX_DAYS)


async def _token_out(session: AsyncSession, row: PersonalAccessToken) -> dict[str, Any]:
    bots = list(await session.scalars(select(Bot).where(Bot.id.in_(row.bot_ids))))
    expired = row.expires_at <= utcnow()
    status = "revoked" if row.revoked_at else ("expired" if expired else "active")
    return {
        "id": str(row.id),
        "name": row.name,
        "prefix": row.token_prefix,
        "bots": [
            {"id": str(b.id), "name": b.name, "bot_key": b.bot_key, "api_access": b.api_access}
            for b in sorted(bots, key=lambda b: (b.name, str(b.id)))
        ],
        "status": status,
        "expires_at": row.expires_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/api/me/api-tokens")
async def list_tokens(
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    rows = await session.scalars(
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == actor.id)
        .order_by(PersonalAccessToken.created_at.desc())
    )
    return {"code": 0, "data": [await _token_out(session, row) for row in rows]}


@router.get("/api/me/api-token-bots")
async def token_bot_options(
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """建令牌时可选的 AI 员工：已启用的飞书 AI 员工里本人能用的。没开 API 驱动的也列出，标出来。"""
    bots = await session.scalars(
        select(Bot).where(Bot.platform == "feishu", Bot.enabled.is_(True)).order_by(Bot.name)
    )
    data = [
        {"id": str(b.id), "name": b.name, "bot_key": b.bot_key, "api_access": b.api_access}
        for b in bots
        if await service.allowed_to_use(session, b, actor.id)
    ]
    return {"code": 0, "data": data}


@router.post("/api/me/api-tokens", status_code=201, dependencies=[Depends(verify_csrf)])
async def create_token(
    body: TokenIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    try:
        minted = await tokens.mint(
            session, actor, name=body.name, bot_ids=body.bot_ids, days=body.days
        )
    except TokenError as exc:
        raise _error(exc) from None
    await record_audit(
        session,
        action="api_token.create",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="api_token",
        target_id=str(minted.row.id),
        diff={
            "name": minted.row.name,
            "bot_ids": [str(b) for b in minted.row.bot_ids],
            "expires_at": minted.row.expires_at.isoformat(),
        },
        ip=client_ip(request),
    )
    data = await _token_out(session, minted.row)
    await session.commit()
    return {"code": 0, "data": {**data, "token": minted.plaintext}}


@router.delete("/api/me/api-tokens/{token_id}", dependencies=[Depends(verify_csrf)])
async def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """吊销立即生效：之后的调用一律 401，排队中的任务开轮时也会结掉。"""
    row = await session.get(PersonalAccessToken, token_id, with_for_update=True)
    if row is None or row.user_id != actor.id:
        raise not_found("令牌不存在")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await record_audit(
            session,
            action="api_token.revoke",
            actor_id=actor.id,
            actor_login=actor.login_name,
            target_type="api_token",
            target_id=str(row.id),
            diff={"name": row.name},
            ip=client_ip(request),
        )
    data = await _token_out(session, row)
    await session.commit()
    return {"code": 0, "data": data}
