"""通过 API 驱动 AI 员工：外部程序用个人令牌发起、查询、取消；本人在管理台管理自己的令牌。

见 docs/superpowers/specs/2026-11-06-api-driven-tasks.md。令牌明文只在创建的响应里出现一次。
"""

from __future__ import annotations

import hashlib
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
from coreman.core.api_tasks import groups, service, tokens, uploads
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    ApiClient,
    ApiTask,
    ApiTaskUpload,
    Bot,
    PersonalAccessToken,
    User,
)
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["api-tasks"])


def _error(exc: TokenError) -> ApiError:
    errors = [{"type": exc.error}] if exc.error else None
    return ApiError(exc.status, exc.status, exc.message, errors)


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
    # 员工邮箱前缀。调用方为这个 AI 员工配了群承接时不能带（执行身份固定为服务账号）。
    user: str | None = Field(default=None, min_length=1, max_length=128)
    bot_key: str = Field(min_length=1, max_length=50)
    # 群承接模式在哪个群发起；只配了一个群时可省略。
    chat_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=service.PROMPT_MAX)
    request_id: str | None = Field(default=None, min_length=1, max_length=service.REQUEST_ID_MAX)
    # 先用 /api/infra/tasks/uploads 逐张上传拿到的 upload_id，按顺序；张数不限。
    images: list[uuid.UUID] = Field(default_factory=list)


class ClientUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 群承接模式的任务不带。
    user: str | None = Field(default=None, min_length=1, max_length=128)


tasks_client = require_scope(service.CLIENT_SCOPE)
tasks_upload_client = require_scope(service.CLIENT_SCOPE, upload=True)


@router.post("/api/infra/tasks/uploads", status_code=201)
async def upload_client_image(
    request: Request,
    client: ApiClient = Depends(tasks_upload_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """一次一张：表单字段 `user`、`sha256`（都在签名里）和文件 `file`。

    群承接模式不带 `user`，改带 `bot_key`（多个承接群时再带 `chat_id`）：图片归配置里的服务
    账号，发起时按同一个身份认领。"""
    form = await request.form()
    subject, digest, upload = form.get("user"), form.get("sha256"), form.get("file")
    bot_key, chat_id = form.get("bot_key"), form.get("chat_id")
    group = subject is None and isinstance(bot_key, str) and bool(bot_key)
    if (
        not (isinstance(subject, str) or group)
        or not isinstance(digest, str)
        or isinstance(upload, str)
        or (chat_id is not None and not isinstance(chat_id, str))
    ):
        raise ApiError(422, 422, "需要表单字段 user（群承接模式为 bot_key）、sha256 和文件 file")
    if upload is None:
        raise ApiError(422, 422, "需要文件 file")
    data = await upload.read(uploads.MAX_BYTES + 1)
    if len(data) > uploads.MAX_BYTES:
        raise ApiError(413, 413, f"单张图片不能超过 {uploads.MAX_BYTES // 1024 // 1024}MB")
    # 文件不在签名里：靠签过名的 sha256 绑定，对不上就当签名无效。
    if hashlib.sha256(data).hexdigest() != digest.strip().lower():
        raise ApiError(401, 401, "接口签名无效或已过期")
    try:
        if group:
            assert isinstance(bot_key, str)
            user = await groups.upload_owner(
                session, client, bot_key, chat_id if isinstance(chat_id, str) else None
            )
        else:
            assert isinstance(subject, str)
            if isinstance(bot_key, str) and await groups.routes_for(
                session, client.app_key, (await service.bot_by_key(session, bot_key)).id
            ):
                raise TokenError(422, "这个 AI 员工走群承接模式，上传图片不能带 user")
            user = await service.resolve_member(session, subject)
        row = await uploads.save(session, client_key=client.app_key, user_id=user.id, data=data)
    except TokenError as exc:
        await session.rollback()
        raise _error(exc) from None
    out = {
        "upload_id": str(row.id),
        "sha256": row.sha256,
        "bytes": row.size,
        "content_type": row.content_type,
        "expires_at": row.expires_at.isoformat(),
    }
    await session.commit()
    return {"code": 0, "data": out}


@router.get("/api/infra/tasks/uploads/{upload_id}/raw")
async def client_image_raw(
    upload_id: uuid.UUID,
    request: Request,
    token: str = Query(min_length=1, max_length=2048),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """AI 员工下载第 9 张以后的图片：链接里的令牌是平台签的，24 小时有效。"""
    if not uploads.link_valid(request.app.state.cipher, upload_id, token, utcnow()):
        raise not_found("图片不存在或链接已过期")
    row = await session.get(ApiTaskUpload, upload_id)
    if row is None:
        raise not_found("图片不存在或链接已过期")
    return Response(
        content=bytes(row.data),
        media_type=row.content_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


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
            images=body.images,
            chat_id=body.chat_id,
        )
    except TokenError as exc:
        await session.rollback()
        raise _error(exc) from None
    if created:
        group = groups.is_group_row(row)
        actor = await session.get(User, row.user_id) if group else None
        await record_audit(
            session,
            action="api_task.create",
            actor_id=row.user_id,
            actor_login=(
                actor.login_name if actor is not None else (body.user or "").strip().lower()
            ),
            target_type="bot",
            target_id=str(row.bot_id),
            diff={
                "api_task_id": str(row.id),
                "api_client": client.app_key,
                "images": len(row.image_ids),
                **({"chat_id": row.chat_id} if group else {}),
            },
            ip=client_ip(request),
        )
    data = await service.view(session, row)
    await session.commit()
    if not created:
        response.status_code = 200
    return {"code": 0, "data": data}


async def _client_task(
    session: AsyncSession, client: ApiClient, task_id: uuid.UUID, subject: str | None
) -> ApiTask:
    """只看得到这个调用方自己发起的任务：私聊模式还要是为这位员工发起的。"""
    row = await session.get(ApiTask, task_id)
    if row is None or row.client_key != client.app_key:
        raise not_found("任务不存在")
    if groups.is_group_row(row):
        if subject is not None:
            raise ApiError(422, 422, "群承接模式的任务不带 user")
        return row
    if subject is None:
        raise ApiError(422, 422, "需要 user（员工邮箱前缀）")
    user = await session.get(User, row.user_id)
    prefix = (user.email or "").split("@", 1)[0].lower() if user else ""
    if not prefix or prefix != subject.strip().lower():
        raise not_found("任务不存在")
    return row


@router.get("/api/infra/tasks/run/{task_id}")
async def get_client_task(
    task_id: uuid.UUID,
    user: str | None = Query(default=None, min_length=1, max_length=128),
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
