"""飞书求助仅消费引用当前求助通知的私聊回复，不挪用普通聊天或生成可达记录。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, InboundEvent, PlatformApp, User
from coreman.core.escalations.service import add_reply
from coreman.core.prompting.system_prompt import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext


async def consume_reply(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    speaker: Speaker,
    parts: list[dict[str, Any]],
    content: str,
) -> bool:
    if bot.platform != "feishu" or inbound.chat_type != "single" or not speaker.user_id:
        return False
    parent = inbound.reply_context.get("parent_id")
    if not parent:
        return False
    credentials = decrypt_json(ctx.cipher, bot.credentials_enc, CREDENTIALS_AAD)
    apps = list(
        await session.scalars(
            select(PlatformApp).where(
                PlatformApp.platform == "feishu",
                PlatformApp.enabled,
                PlatformApp.app_id == credentials.get("app_id"),
                PlatformApp.capabilities.contains(["notify", "callback"]),
            )
        )
    )
    if len(apps) != 1:
        return False
    user = await session.get(User, speaker.user_id)
    if user is None or user.status != "active" or user.source == "bootstrap":
        return False
    now = utcnow()
    try:
        created = datetime.fromtimestamp(
            int(inbound.reply_context.get("create_time", "")) / 1000, UTC
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    if created > now + timedelta(minutes=5):
        return False
    media = [part for part in parts if part.get("type") in {"image", "file"}]
    # 先不消费不支持的混合媒体，让正常消息处理给出明确反馈。
    if len(media) > 1 or any(part.get("type") not in {"text", "image", "file"} for part in parts):
        return False
    descriptor = media[0] if media else None
    if not content.strip() and descriptor is None:
        return False
    row = await add_reply(
        session,
        user=user,
        app=apps[0],
        content=content or "附件处理中",
        msg_id=inbound.platform_msg_id,
        created_at=created,
        now=now,
        parent_message_id=str(parent),
        media={"type": descriptor["type"], "status": "pending"} if descriptor else None,
    )
    if row is None:
        return False
    if descriptor:
        ref = descriptor.get("ref") or {}
        if ref.get("message_id") != inbound.platform_msg_id or not ref.get("file_key"):
            raise ValueError("invalid escalation resource")
        await tasks.enqueue(
            session,
            NewTask(
                bot_id=bot.id,
                user_id=user.id,
                kind="escalation_media",
                dedupe_key=f"escalation-media:{apps[0].id}:{inbound.platform_msg_id}",
                payload={
                    "platform_app_id": str(apps[0].id),
                    "escalation_id": row.escalation_id,
                    "media_type": descriptor["type"],
                    "media_id": ref["file_key"],
                    "message_id": inbound.platform_msg_id,
                    "caption": content,
                },
            ),
        )
    await tasks.finish(
        session, ctx.task.id, status="succeeded", result={"escalation_id": row.escalation_id}
    )
    return True
