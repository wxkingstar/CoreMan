"""把定时结果按渠道写入同一 outbox，不在任务事务中调用外部平台。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp, User, UserIdentity, UserReached
from coreman.core.i18n.messages import msg

# 各渠道单条字节上限与最多条数。结果正文本身已限 10 万字符（cron_handler），但按这个上限
# 分片，群 webhook 能刷出上百条、通知应用更多；超过条数就截断并附提示，完整结果留在运行记录。
CHAT_PART_BYTES, CHAT_MAX_PARTS = 20000, 5
NOTIFY_PART_BYTES, NOTIFY_MAX_PARTS = 2048, 5
WEBHOOK_PART_BYTES, WEBHOOK_MAX_PARTS = 4096, 10
# 分片按 UTF-8 边界切，每片最多比上限少 3 个字节；预算里按每片 4 字节扣掉，保证不超条数。
_BOUNDARY_SLACK = 4


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


def bounded_chunks(text: str, limit: int, max_parts: int, notice: str) -> list[str]:
    """按 `limit` 字节分片，最多 `max_parts` 片；放不下时截断正文并在末尾附 `notice`。"""
    data = text.encode("utf-8")
    if len(data) <= limit * max_parts - _BOUNDARY_SLACK * max_parts:
        return chunks(text, limit)
    budget = limit * max_parts - _BOUNDARY_SLACK * max_parts - len(notice.encode("utf-8"))
    if budget <= 0:
        raise ValueError("chunk budget too small")
    head = data[:budget].decode("utf-8", errors="ignore")
    return chunks(head + notice, limit)


async def enqueue_result(
    session: AsyncSession,
    *,
    bot: Bot,
    config: dict[str, Any],
    run_id: int | str,
    content: str,
    cipher: Cipher,
    locale: str = "zh",
    fallback_user_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """按快照里的接收人与渠道入队；返回 `{outbox_ids, errors}`。

    `fallback_user_id`（通常是任务创建者）：一个接收人、群、邮箱、webhook 都没配时，结果
    改按私聊推给这个人，免得任务跑成功却无人收到。测试通知不传它，照旧提示先配置接收人。
    """
    ids: list[int] = []
    errors: dict[str, str] = {}
    notice = msg("cron_delivery_truncated", locale)
    webhook_enc = config.get("notify_webhook_url_enc")
    # 仅兼容升级前已入队的任务快照；新任务总会包含独立地址字段。
    if "notify_webhook_url_enc" not in config and bot.notify_webhook_url:
        webhook_enc = cipher.encrypt(bot.notify_webhook_url, "notifications.webhook_url")
    recipients = list(dict.fromkeys(config.get("target_users", [])))
    has_channel = bool(
        recipients
        or config.get("target_chats")
        or config.get("notify_emails")
        or (config.get("notify_webhook") and webhook_enc)
    )
    fallback = not has_channel and fallback_user_id is not None
    if fallback:
        recipients = [str(fallback_user_id)]
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
            parts = bounded_chunks(content, CHAT_PART_BYTES, CHAT_MAX_PARTS, notice)
            for index, part in enumerate(parts):
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
        limit = max(256, NOTIFY_PART_BYTES - len(hint.encode()))
        for index, part in enumerate(bounded_chunks(content, limit, NOTIFY_MAX_PARTS, notice)):
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
        for index, part in enumerate(
            bounded_chunks(content, CHAT_PART_BYTES, CHAT_MAX_PARTS, notice)
        ):
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
    if config.get("notify_webhook") and webhook_enc:
        # 密钥 URL 不进入可见的 outbox target/API；发送时解密，异常不保存 URL。
        target = {
            "channel": "webhook",
            "url_enc": webhook_enc,
        }
        for index, part in enumerate(
            bounded_chunks(content, WEBHOOK_PART_BYTES, WEBHOOK_MAX_PARTS, notice)
        ):
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
    result: dict[str, Any] = {"outbox_ids": ids, "errors": errors}
    if fallback:
        result["fallback_user_id"] = str(fallback_user_id)
    return result
