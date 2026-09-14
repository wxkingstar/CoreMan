"""把定时结果按渠道写入同一 outbox，不在任务事务中调用外部平台。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User, UserIdentity, UserReached
from coreman.runtime.bus import outbox


def chunks(text: str, limit: int) -> list[str]:
    remaining = text.encode("utf-8")
    result = []
    while remaining:
        part = remaining[:limit].decode("utf-8", errors="ignore")
        if not part:
            raise ValueError("chunk limit too small")
        result.append(part)
        remaining = remaining[len(part.encode("utf-8")) :]
    return result


async def enqueue_result(
    session: AsyncSession,
    *,
    bot: Bot,
    config: dict[str, Any],
    run_id: int | str,
    content: str,
    cipher: Cipher,
) -> dict[str, Any]:
    ids: list[int] = []
    errors: dict[str, str] = {}
    recipients = list(dict.fromkeys(config.get("target_users", [])))
    for uid in recipients:
        user = await session.get(User, uuid.UUID(uid))
        if user is None or user.status != "active" or user.source == "bootstrap":
            errors[f"user:{uid}"] = "recipient_disabled"
            continue
        ident = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id, UserIdentity.platform == bot.platform
            )
        )
        if ident is None:
            errors[f"user:{uid}"] = "recipient_unbound"
            continue
        reached = await session.get(UserReached, (bot.id, user.id))
        if reached is not None:
            for index, part in enumerate(chunks(content, 20000)):
                item = await outbox.add(
                    session,
                    bot_id=bot.id,
                    platform=bot.platform,
                    kind="send",
                    dedupe_key=f"cron:{run_id}:dm:{uid}:{index}",
                    target={
                        "chat_id": reached.platform_chat_id,
                        "recipient_user_id": uid,
                        "recipient_platform_user_id": ident.platform_user_id,
                    },
                    payload={"markdown": part},
                )
                if item:
                    ids.append(item.id)
            continue
        apps = (
            await session.scalars(
                select(PlatformApp).where(
                    PlatformApp.platform == bot.platform,
                    PlatformApp.enabled,
                    PlatformApp.capabilities.contains(["notify"]),
                )
            )
        ).all()
        if ident is None or len(apps) != 1:
            errors[f"user:{uid}"] = "no_private_chat_or_unambiguous_notification_app"
            continue
        hint = f"\n\n请先给机器人「{bot.name}」发一句话建立私聊。"
        for index, part in enumerate(chunks(content, max(256, 2048 - len(hint.encode())))):
            item = await outbox.add(
                session,
                bot_id=None,
                platform=bot.platform,
                kind="notify",
                dedupe_key=f"cron:{run_id}:fallback:{uid}:{index}",
                target={
                    "platform_app_id": str(apps[0].id),
                    "user_id": uid,
                    "platform_user_id": ident.platform_user_id,
                },
                payload={"content": part + hint, "msgtype": "text"},
            )
            if item:
                ids.append(item.id)
    for chat_id in dict.fromkeys(config.get("target_chats", [])):
        for index, part in enumerate(chunks(content, 20000)):
            item = await outbox.add(
                session,
                bot_id=bot.id,
                platform=bot.platform,
                kind="send",
                dedupe_key=f"cron:{run_id}:group:{chat_id}:{index}",
                target={"chat_id": chat_id},
                payload={"markdown": part},
            )
            if item:
                ids.append(item.id)
    webhook_enc = config.get("notify_webhook_url_enc")
    # 仅兼容升级前已入队的任务快照；新任务总会包含独立地址字段。
    if "notify_webhook_url_enc" not in config and bot.notify_webhook_url:
        webhook_enc = cipher.encrypt(bot.notify_webhook_url, "notifications.webhook_url")
    if config.get("notify_webhook") and webhook_enc:
        # 密钥 URL 不进入可见的 outbox target/API；发送时解密，异常不保存 URL。
        target = {
            "channel": "webhook",
            "url_enc": webhook_enc,
        }
        for index, part in enumerate(chunks(content, 4096)):
            item = await outbox.add(
                session,
                bot_id=None,
                platform="wecom",
                kind="notify",
                dedupe_key=f"cron:{run_id}:webhook:{index}",
                target=target,
                payload={"content": part},
            )
            if item:
                ids.append(item.id)
    for email in dict.fromkeys(config.get("notify_emails", [])):
        item = await outbox.add(
            session,
            bot_id=None,
            platform="email",
            kind="notify",
            dedupe_key=f"cron:{run_id}:email:{email}",
            target={"channel": "email", "email": email},
            payload={"subject": f"CoreMan · {config.get('name', bot.name)}", "content": content},
        )
        if item:
            ids.append(item.id)
    return {"outbox_ids": ids, "errors": errors}
