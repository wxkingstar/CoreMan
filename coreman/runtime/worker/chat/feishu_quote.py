"""把飞书 parent_id 安全地补成文本引用；不读取父消息媒体或任意 URL。"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuDelivery, OutboxItem, TaskStream
from coreman.core.platforms.feishu import FeishuClient

MAX_QUOTE_TEXT = 12_000
UNAVAILABLE = "[引用消息内容不可用]"
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _bounded(text: str) -> str | None:
    value = text.strip()
    if not value:
        return None
    return value[:MAX_QUOTE_TEXT]


async def _persisted_text(
    session: AsyncSession, *, bot: Bot, chat_id: str, parent_id: str
) -> str | None:
    final_text = await session.scalar(
        select(TaskStream.final_text)
        .join(FeishuDelivery, FeishuDelivery.task_id == TaskStream.task_id)
        .where(
            FeishuDelivery.message_id == parent_id,
            TaskStream.bot_id == bot.id,
            TaskStream.platform == "feishu",
            TaskStream.reply_context["chat_id"].astext == chat_id,
            TaskStream.final_text.is_not(None),
        )
        .order_by(TaskStream.task_id.desc())
        .limit(1)
    )
    if isinstance(final_text, str) and (value := _bounded(final_text)):
        return value
    markdown = await session.scalar(
        select(OutboxItem.payload["markdown"].astext)
        .where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.platform == "feishu",
            OutboxItem.status == "sent",
            OutboxItem.target["chat_id"].astext == chat_id,
            OutboxItem.payload["_feishu_message_id"].astext == parent_id,
        )
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    return _bounded(markdown) if isinstance(markdown, str) else None


def _api_text(body: dict[str, Any], *, chat_id: str, parent_id: str) -> str | None:
    data = body.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    if item.get("message_id") != parent_id or item.get("chat_id") != chat_id:
        return None
    raw = (item.get("body") or {}).get("content")
    if not isinstance(raw, str) or len(raw) > 256_000:
        return None
    try:
        content = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(content, dict):
        return None
    if isinstance(content.get("text"), str):
        return _bounded(content["text"])
    if item.get("msg_type") == "interactive":
        card_pieces: list[str] = []
        pending: list[tuple[object, int]] = [(content, 0)]
        visited = 0
        while pending and visited < 500:
            value, depth = pending.pop()
            visited += 1
            if depth > 8:
                continue
            if isinstance(value, dict):
                for key, child in list(value.items())[:100]:
                    if key in {"text", "content", "title"} and isinstance(child, str):
                        if child.strip():
                            card_pieces.append(child.strip())
                    elif isinstance(child, (dict, list)):
                        pending.append((child, depth + 1))
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value[:100])
        return _bounded("\n".join(reversed(card_pieces)))
    # post 消息只采集官方结构里的文本节点；图片、文件 key 和 URL 一概不碰。
    pieces: list[str] = []
    for localized in list(content.values())[:4]:
        rows = localized.get("content") if isinstance(localized, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows[:100]:
            if not isinstance(row, list):
                continue
            for node in row[:100]:
                if isinstance(node, dict) and node.get("tag") in {"text", "md"}:
                    value = node.get("text")
                    if isinstance(value, str) and value.strip():
                        pieces.append(value.strip())
    return _bounded("\n".join(pieces))


async def enrich(
    session: AsyncSession,
    *,
    bot: Bot,
    cipher: Cipher,
    chat_id: str,
    parent_id: object,
    parts: list[dict[str, Any]],
    text: str,
) -> tuple[list[dict[str, Any]], str]:
    """返回补过引用的 parts/text；失败显式标注，但不伪造父消息原文。"""
    if not isinstance(parent_id, str) or not _MESSAGE_ID.fullmatch(parent_id) or not chat_id:
        note_text = f"{UNAVAILABLE}\n\n{text}" if text.strip() else UNAVAILABLE
        return [{"type": "text", "text": UNAVAILABLE}, *parts], note_text
    if any(part.get("type") == "quote" for part in parts):
        return parts, text
    quoted = await _persisted_text(session, bot=bot, chat_id=chat_id, parent_id=parent_id)
    if quoted is None:
        # 不在官方 HTTP 往返期间占着数据库事务/连接。
        await session.rollback()
        client: FeishuClient | None = None
        try:
            credentials = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
            client = FeishuClient(
                credentials.get("app_id", ""), credentials.get("app_secret", "")
            )
            response = await client.call("GET", f"/open-apis/im/v1/messages/{parent_id}")
            quoted = _api_text(response, chat_id=chat_id, parent_id=parent_id)
        except Exception:
            # 解密、凭证、权限、删除、超时和畸形响应都统一显式降级；绝不反射上游错误。
            quoted = None
        finally:
            if client is not None:
                await client.aclose()
    if quoted is not None:
        return [*parts, {"type": "quote", "kind": "text", "text": quoted, "refs": []}], text
    note_text = f"{UNAVAILABLE}\n\n{text}" if text.strip() else UNAVAILABLE
    return [{"type": "text", "text": UNAVAILABLE}, *parts], note_text
