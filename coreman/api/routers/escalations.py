"""人工求助及旧接口别名；签名调用方隔离和真实发起者归属。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.infra_auth import signed_client
from coreman.api.security import SESSION_COOKIE
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import DecryptError
from coreman.core.db.models import ApiClient, Bot, Escalation, PlatformApp, User, UserIdentity
from coreman.core.escalations import service
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["escalations"])


async def escalation_client(
    request: Request, session: AsyncSession = Depends(get_session)
) -> ApiClient:
    # 只接受签名调用；旧 X-API-Key 直接携带客户端 secret 的方式已移除。
    client = await signed_client(request, session)
    if "escalations" not in client.scopes:
        raise ApiError(403, 403, "调用方没有求助权限")
    await session.execute(
        update(ApiClient).where(ApiClient.app_key == client.app_key).values(last_used_at=utcnow())
    )
    await session.commit()
    return client


async def optional_actor(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> User | None:
    if request.cookies.get("bot_token") or request.cookies.get(SESSION_COOKIE):
        return await current_user(request, response, session)
    return None


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_user_id: str = Field(min_length=1, max_length=128)
    to_real_name: str = Field(default="", max_length=100)


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_key: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=1, max_length=8000)
    from_user_id: str = Field(default="", max_length=128)
    to_user_id: str = Field(default="", max_length=128)
    to_real_name: str = Field(default="", max_length=100)
    target_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    targets: list[Target] = Field(default_factory=list, max_length=20)
    platform_app_id: uuid.UUID | None = None
    platform: Literal["wecom", "feishu"] = "wecom"
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_selector(self) -> CreateIn:
        if sum(bool(x) for x in (self.to_user_id, self.target_user_ids, self.targets)) != 1:
            raise ValueError("请指定一种接收人列表")
        return self


def _out(row: Escalation) -> dict[str, Any]:
    return {
        "escalation_id": row.escalation_id,
        "group_id": row.group_id,
        "status": row.status,
        "queued": row.status == "queued",
        "question": row.question,
        "replies": row.replies,
        "rounds": row.rounds,
        "resolution": row.resolution,
        "from_user_id": row.from_platform_user_id,
        "to_user_id": row.to_platform_user_id,
        "sender_user_id": str(row.from_user_id) if row.from_user_id else None,
        "recipient_user_id": str(row.to_user_id),
        "followup_questions": row.followup_questions,
        "expires_at": row.expires_at,
        "last_reply_at": row.last_reply_at,
    }


def _group(rows: list[Escalation]) -> dict[str, Any]:
    # 胜出者进入追问 pending 或结束后仍是原回复者，不能按第一行重新选人。
    winner = next((r for r in rows if r.replies), None)
    chosen = winner or rows[0]
    return {
        **_out(chosen),
        "escalations": [_out(row) for row in rows],
        "winner_id": winner.escalation_id if winner else None,
    }


@router.post("/api/infra/escalations")
@router.post("/api/escalation/create")
async def create_escalation(
    body: CreateIn,
    request: Request,
    client: ApiClient = Depends(escalation_client),
    actor: User | None = Depends(optional_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await session.scalar(select(Bot).where(Bot.bot_key == body.bot_key, Bot.enabled))
    if bot is None:
        raise not_found("启用的机器人不存在")
    if actor is not None and actor.source == "bootstrap":
        raise ApiError(403, 403, "引导管理员不能代表员工发起求助")
    if body.from_user_id:
        ident = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.platform == bot.platform,
                UserIdentity.platform_user_id == body.from_user_id,
            )
        )
        if actor is None or ident is None or ident.user_id != actor.id:
            raise ApiError(403, 403, "不能用请求参数冒充求助发起人，请携带本人身份令牌")
    query = (
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            User.status == "active",
            User.source != "bootstrap",
            UserIdentity.platform == body.platform,
        )
    )
    if body.target_user_ids:
        requested = set(body.target_user_ids)
        query = query.where(User.id.in_(requested))
        requested_count = len(requested)
    else:
        platform_ids = {t.to_user_id for t in body.targets} if body.targets else {body.to_user_id}
        if any("|" in value or value == "@all" for value in platform_ids):
            raise ApiError(422, 422, "求助接收人不能是广播或组合账号")
        query = query.where(UserIdentity.platform_user_id.in_(platform_ids))
        requested_count = len(platform_ids)
    recipients = list(await session.scalars(query))
    if len(recipients) != requested_count:
        raise ApiError(422, 422, "部分接收人不存在、停用或未绑定目标平台")
    app_query = select(PlatformApp).where(
        PlatformApp.platform == body.platform,
        PlatformApp.enabled,
        PlatformApp.capabilities.contains(["notify", "callback"]),
    )
    if body.platform_app_id:
        app_query = app_query.where(PlatformApp.id == body.platform_app_id)
    apps = list(await session.scalars(app_query))
    if len(apps) != 1:
        raise ApiError(409, 409, "请指定唯一启用的通知与回调应用")
    app = apps[0]
    if app.platform == "wecom" and (
        not app.corp_id
        or not app.app_id
        or not app.app_id.isdecimal()
        or not app.callback_token_enc
        or not app.callback_aes_key_enc
    ):
        raise ApiError(409, 409, "通知应用的回调配置不完整")
    if app.platform == "feishu":
        receivers = 0
        for gateway_bot in await session.scalars(
            select(Bot).where(Bot.platform == "feishu", Bot.enabled)
        ):
            try:
                credentials = decrypt_json(
                    request.app.state.cipher, gateway_bot.credentials_enc, CREDENTIALS_AAD
                )
            except (DecryptError, ValueError):
                continue
            if credentials.get("app_id") == app.app_id:
                receivers += 1
        if receivers != 1:
            raise ApiError(409, 409, "飞书通知应用须关联唯一启用的机器人以接收回复")
    rows = await service.create(
        session,
        client_key=client.app_key,
        bot=bot,
        sender=actor,
        recipients=recipients,
        app=app,
        question=body.question,
        request_id=body.request_id,
        now=utcnow(),
    )
    await session.commit()
    return {"code": 0, "data": _group(rows)}


@router.get("/api/infra/escalations/{identity}")
@router.get("/api/escalation/{identity}/poll")
async def poll(
    identity: str,
    client: ApiClient = Depends(escalation_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await service.load(session, identity, client.app_key)
    now = utcnow()
    for row in rows:
        if row.status in service.OPEN:
            if service.is_expired(row, now):
                await service.close(session, row, "expired", "expired", now)
            else:
                row.last_polled_at = now
    await session.commit()
    return {"code": 0, "data": _group(rows)}


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["agent", "observed", "offline"] = "agent"


@router.post("/api/infra/escalations/{identity}/resolve")
@router.post("/api/escalation/{identity}/resolve")
async def resolve(
    identity: str,
    body: ResolveIn,
    client: ApiClient = Depends(escalation_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await service.load(session, identity, client.app_key)
    now = utcnow()
    if body.resolution == "agent" and not any(row.replies for row in rows):
        raise ApiError(409, 409, "尚无回复，请使用 observed 或 offline 说明实际收束原因")
    for row in rows:
        expired = service.is_expired(row, now)
        await service.close(
            session,
            row,
            "expired" if expired else "completed",
            "expired" if expired else body.resolution,
            now,
            activate=False,
        )
    for uid in {row.to_user_id for row in rows}:
        await service.activate_next(session, uid, now)
    await session.commit()
    return {"code": 0, "data": _group(rows)}


class FollowupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=8000)


@router.post("/api/infra/escalations/{identity}/followup")
@router.post("/api/escalation/{identity}/followup")
async def followup(
    identity: str,
    body: FollowupIn,
    client: ApiClient = Depends(escalation_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await service.load(session, identity, client.app_key)
    chosen = next((row for row in rows if row.status in service.ACTIVE and row.replies), None)
    if chosen is None:
        raise ApiError(409, 409, "尚未收到可追问的回复")
    await service.followup(session, chosen, body.question, datetime.now(UTC))
    await session.commit()
    return {"code": 0, "data": _group(rows)}


@router.post("/api/infra/escalations/{identity}/cancel")
@router.post("/api/escalation/{identity}/cancel")
async def cancel(
    identity: str,
    client: ApiClient = Depends(escalation_client),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await service.load(session, identity, client.app_key)
    now = utcnow()
    for row in rows:
        await service.close(session, row, "cancelled", None, now, activate=False)
    for uid in {row.to_user_id for row in rows}:
        await service.activate_next(session, uid, now)
    await session.commit()
    return {"code": 0, "data": _group(rows)}
