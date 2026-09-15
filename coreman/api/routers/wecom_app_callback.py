"""企微应用回调：解密、归属核验、去重、入库后快速应答。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.db.models import AuthNonce, PlatformApp, User, UserIdentity
from coreman.core.escalations import service
from coreman.core.timeutils import utcnow
from coreman.core.wecom.callback_crypto import MAX_BODY, CallbackCrypto, CallbackError, parse_xml

router = APIRouter(tags=["wecom-callback"])


async def _app(session: AsyncSession, app_id: uuid.UUID | None) -> PlatformApp:
    query = select(PlatformApp).where(
        PlatformApp.platform == "wecom",
        PlatformApp.enabled,
        PlatformApp.capabilities.contains(["callback"]),
    )
    if app_id:
        query = query.where(PlatformApp.id == app_id)
    apps = list(await session.scalars(query))
    if len(apps) != 1:
        raise ApiError(403, 403, "回调应用不可用或不唯一")
    app = apps[0]
    if (
        not app.corp_id
        or not app.app_id
        or not app.callback_token_enc
        or not app.callback_aes_key_enc
    ):
        raise ApiError(403, 403, "回调配置不完整")
    return app


def _crypto(request: Request, app: PlatformApp) -> CallbackCrypto:
    cipher = request.app.state.cipher
    assert app.callback_token_enc and app.callback_aes_key_enc and app.corp_id
    return CallbackCrypto(
        cipher.decrypt(app.callback_token_enc, "platform_apps.callback_token_enc"),
        cipher.decrypt(app.callback_aes_key_enc, "platform_apps.callback_aes_key_enc"),
        app.corp_id,
    )


def _decrypt(request: Request, app: PlatformApp, encrypted: str) -> bytes:
    params = request.query_params
    try:
        return _crypto(request, app).decrypt(
            encrypted,
            signature=params.get("msg_signature", ""),
            timestamp=params.get("timestamp", ""),
            nonce=params.get("nonce", ""),
        )
    except (CallbackError, ValueError):
        raise ApiError(403, 403, "回调验证失败") from None


@router.get("/api/callbacks/wecom/{app_id}")
@router.get("/weixin/app/callback")
@router.get("/callbacks/wecom/app")
async def verify(
    request: Request, app_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> Response:
    app = await _app(session, app_id)
    raw = _decrypt(request, app, request.query_params.get("echostr", ""))
    return Response(content=raw, media_type="text/plain")


@router.post("/api/callbacks/wecom/{app_id}")
@router.post("/weixin/app/callback")
@router.post("/callbacks/wecom/app")
async def receive(
    request: Request, app_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> Response:
    app = await _app(session, app_id)
    raw = b""
    async for part in request.stream():
        raw += part
        if len(raw) > MAX_BODY:
            raise ApiError(413, 413, "回调报文过大")
    try:
        envelope = parse_xml(raw)
        body = parse_xml(_decrypt(request, app, envelope.get("Encrypt", "")))
    except CallbackError:
        raise ApiError(403, 403, "回调报文无效") from None
    # 密文中的应用和企业字段才是可信归属；外层 XML 的字段不参与这项授权。
    if body.get("ToUserName") != app.corp_id or body.get("AgentID") != app.app_id:
        raise ApiError(403, 403, "回调所属企业或应用不匹配")
    sender = body.get("FromUserName", "")
    user = await session.scalar(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.platform == "wecom",
            UserIdentity.platform_user_id == sender,
            User.status == "active",
            User.source != "bootstrap",
        )
    )
    if user is None:
        raise ApiError(403, 403, "回调用户不存在或已停用")
    if body.get("MsgType") not in {"text", "image", "voice", "video", "file"}:
        return Response(content="success", media_type="text/plain")
    msg_id, created = body.get("MsgId", ""), body.get("CreateTime", "")
    if (
        not msg_id
        or len(msg_id) > 128
        or not created.isascii()
        or not created.isdecimal()
        or len(created) > 12
    ):
        raise ApiError(403, 403, "回调缺少消息标识或时间")
    now = utcnow()
    try:
        sent_at = datetime.fromtimestamp(int(created), UTC)
    except (ValueError, OverflowError, OSError):
        raise ApiError(403, 403, "回调消息时间无效") from None
    if abs((now - sent_at).total_seconds()) > 600:
        raise ApiError(403, 403, "回调消息已过期")
    content, media = body.get("Content", ""), None
    if body["MsgType"] != "text":
        if not body.get("MediaId") or len(body["MediaId"]) > 512:
            raise ApiError(422, 422, "媒体回调缺少有效 MediaId")
        media = {"type": body["MsgType"], "status": "pending", "media_id": body["MediaId"]}
        content = f"[{body['MsgType']}]"
    if not content.strip() or len(content) > 16000:
        return Response(content="success", media_type="text/plain")
    receipt = f"{app.id}:{msg_id}"
    fresh = await session.scalar(
        insert(AuthNonce)
        .values(kind="wecom_app_callback", value=receipt)
        .on_conflict_do_nothing(index_elements=[AuthNonce.kind, AuthNonce.value])
        .returning(AuthNonce.value)
    )
    if fresh:
        row = await service.add_reply(
            session,
            user=user,
            app=app,
            content=content,
            msg_id=msg_id,
            created_at=sent_at,
            now=now,
            media=media,
        )
        if row is not None and media is not None:
            await enqueue(
                session,
                NewTask(
                    bot_id=row.bot_id,
                    kind="escalation_media",
                    user_id=user.id,
                    dedupe_key=f"escalation-media:{receipt}",
                    payload={
                        "escalation_id": row.escalation_id,
                        "platform_app_id": str(app.id),
                        "media_id": media["media_id"],
                        "media_type": media["type"],
                        "message_id": msg_id,
                    },
                ),
            )
    await session.commit()
    return Response(content="success", media_type="text/plain")
