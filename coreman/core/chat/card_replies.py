"""飞书回复按钮在服务端的约定：点击带来的元数据、会话类型判定、轮次开始后的置灰。

网关应答回调时只落库（3 秒窗口），会话类型按真实私聊记录判定；这一轮通过了入站的各道关
（机器人启用、白名单、身份、命令）真正开始时，worker 才排一条出站把按钮行置灰，被拒的点击
不改卡片。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.db.models import InboundEvent
from coreman.core.feishu_cards.reply_buttons import UsedRow

# 点击产生的入站在 reply_context 里带这一项：被点的卡片消息、按钮行、按钮文字、提问人。
REPLY_BUTTON = "reply_button"
CLICK_EVENT = "card.action.trigger"


def is_click(payload: Mapping[str, Any]) -> bool:
    """这条入站（InboundMessage 的持久化形态）是不是卡片按钮点出来的。"""
    raw = payload.get("raw")
    header = raw.get("header") if isinstance(raw, dict) else None
    return isinstance(header, dict) and header.get("event_type") == CLICK_EVENT


def clicked(reply_context: Mapping[str, Any]) -> dict[str, Any] | None:
    meta = reply_context.get(REPLY_BUTTON)
    return meta if isinstance(meta, dict) else None


async def enqueue_used(session: AsyncSession, bot_id: uuid.UUID, event: InboundEvent) -> None:
    """这一轮开始了：排一条出站，把被点的按钮行置灰并写明谁选了什么（同一行只标一次）。"""
    meta = clicked(event.reply_context or {})
    if meta is None or not is_click(event.payload or {}):
        return
    used = UsedRow.parse({**meta, "chat_type": event.chat_type})
    mid = meta.get("message_id")
    if used is None or not isinstance(mid, str) or not mid:
        return
    await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"feishu-reply-used:{mid}:{used.row}",
        target={"message_id": mid, "chat_id": event.chat_id},
        payload={"_reply_used": used.dump(), "_operator": event.sender_platform_user_id or ""},
    )
