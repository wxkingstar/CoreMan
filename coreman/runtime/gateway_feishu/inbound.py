"""官方已验签/长连接事件归一化；群消息只接受明确 @ 本 bot。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from coreman.core.wecom.cards import parse_task_id
from coreman.core.wecom.messages import (
    AudioPart,
    FilePart,
    ImagePart,
    InboundMessage,
    Part,
    Sender,
    TextPart,
    VideoPart,
)

_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _normalize_event(
    raw: dict[str, Any],
    *,
    bot_id: uuid.UUID,
    app_id: str,
    bot_open_id: str,
    gateway_instance: str,
    now: datetime,
    allow_bot: bool = False,
) -> InboundMessage | None:
    header = raw.get("header") or {}
    if header.get("app_id") != app_id:
        return None
    event = raw.get("event") or {}
    event_type = header.get("event_type")
    context: dict[str, Any] = {"gateway_instance": gateway_instance, "received_at": now.isoformat()}
    if event_type in {
        "im.chat.member.bot.added_v1",
        "im.chat.access_event.bot_p2p_chat_entered_v1",
    }:
        chat_id = str(event.get("chat_id") or "")
        if not chat_id or not header.get("event_id"):
            return None
        operator = event.get("operator_id") or {}
        kind = "group" if event_type == "im.chat.member.bot.added_v1" else "single"
        return InboundMessage(
            platform="feishu",
            bot_id=bot_id,
            kind="enter_chat",
            chat_type=kind,
            chat_id=chat_id,
            sender=Sender(platform_user_id=str(operator.get("user_id") or "")),
            message_id="welcome:" + str(header["event_id"]),
            reply_context={**context, "chat_id": chat_id, "chat_type": kind},
            raw=raw,
        )
    if event_type == "card.action.trigger":
        operator = event.get("operator") or {}
        ctx = event.get("context") or {}
        action = event.get("action") or {}
        value = action.get("value") or {}
        if not isinstance(value, dict):
            return None
        task_id = str(value.get("task_id") or "")
        if (
            not parse_task_id(task_id) and not re.fullmatch(r"personal:[1-9][0-9]{0,18}", task_id)
        ) or not header.get("event_id"):
            return None
        user_id = str(operator.get("user_id") or "")
        chat_id = str(ctx.get("open_chat_id") or "")
        message_id = str(ctx.get("open_message_id") or "")
        if not user_id or not chat_id or not message_id:
            return None
        selected: dict[str, list[str]] = {}
        form = action.get("form_value") or {}
        if not isinstance(form, dict):
            return None
        for key, entry in form.items():
            if isinstance(entry, str):
                selected[str(key)] = [entry]
            elif isinstance(entry, list) and all(isinstance(v, str) for v in entry):
                selected[str(key)] = entry
        return InboundMessage(
            platform="feishu",
            bot_id=bot_id,
            kind="card_action",
            chat_type="group",
            chat_id=chat_id,
            sender=Sender(platform_user_id=user_id, open_id=operator.get("open_id")),
            message_id="action:" + str(header["event_id"]),
            card_action={
                "task_id": task_id,
                "card_type": "form",
                "level": str(value.get("level") or ""),
                "event_key": str(value.get("event_key") or ""),
                "selected": selected,
            },
            reply_context={**context, "chat_id": chat_id, "message_id": message_id},
            raw=raw,
        )
    if event_type != "im.message.receive_v1":
        return None
    sender = event.get("sender") or {}
    sender_type = sender.get("sender_type")
    if sender_type != "user" and not (allow_bot and sender_type == "bot"):
        return None
    identity = sender.get("sender_id") or {}
    # user_id 是租户内稳定 ID；应用 open_id 不跨 bot 复用，缺权限时保持未知身份。
    user_id = str(identity.get("user_id") or "") if sender_type == "user" else ""
    message = event.get("message") or {}
    mid, chat_id = str(message.get("message_id") or ""), str(message.get("chat_id") or "")
    chat_type = {"p2p": "single", "group": "group"}.get(str(message.get("chat_type") or ""))
    if not mid or not chat_id or not chat_type:
        return None
    mentions = message.get("mentions") or []
    mentioned = bool(bot_open_id) and any(
        isinstance(m, dict) and (m.get("id") or {}).get("open_id") == bot_open_id for m in mentions
    )
    if chat_type == "group" and not mentioned:
        return None
    try:
        content = json.loads(message.get("content") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(content, dict):
        return None
    parts: list[Part] = []
    kind = str(message.get("message_type") or "")
    if kind == "text":
        text = str(content.get("text") or "")
        for mention in mentions:
            if (
                isinstance(mention, dict)
                and (mention.get("id") or {}).get("open_id") == bot_open_id
            ):
                key = mention.get("key")
                if isinstance(key, str) and key:
                    text = text.replace(key, "")
        parts.append(TextPart(text=text.strip()))
    elif kind == "post":
        post = content
        if "content" not in post and "content_v2" not in post:
            post = next(
                (
                    v
                    for v in content.values()
                    if isinstance(v, dict) and ("content" in v or "content_v2" in v)
                ),
                {},
            )
        title = post.get("title")
        if title:
            parts.append(TextPart(text=str(title)))
        for line in post.get("content_v2", post.get("content")) or []:
            for segment in line if isinstance(line, list) else []:
                if not isinstance(segment, dict):
                    continue
                if segment.get("tag") in {"text", "a", "md", "code_block"}:
                    parts.append(TextPart(text=str(segment.get("text") or "")))
                elif segment.get("tag") == "img" and segment.get("image_key"):
                    parts.append(
                        ImagePart(
                            ref={
                                "message_id": mid,
                                "file_key": segment["image_key"],
                                "type": "image",
                            }
                        )
                    )
        files = post.get("files")
        if isinstance(files, list):
            for entry in files[:100]:
                if not isinstance(entry, dict) or entry.get("is_folder") is not False:
                    continue
                file_key = entry.get("file_key")
                if not isinstance(file_key, str) or not _RESOURCE_ID.fullmatch(file_key):
                    continue
                filename = entry.get("file_name")
                parts.append(
                    FilePart(
                        ref={"message_id": mid, "file_key": file_key, "type": "file"},
                        filename=str(filename) if filename else None,
                    )
                )
    elif kind in {"image", "file"}:
        key = content.get("image_key" if kind == "image" else "file_key")
        if key:
            ref = {"message_id": mid, "file_key": str(key), "type": kind}
            parts.append(
                ImagePart(ref=ref)
                if kind == "image"
                else FilePart(ref=ref, filename=content.get("file_name"))
            )
    elif kind == "audio":
        parts.append(AudioPart(ref={}))
    else:
        parts.append(VideoPart(ref={}))  # 公共 worker 的明确“不支持”回复，不送模型猜测。
    return InboundMessage(
        platform="feishu",
        bot_id=bot_id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender=Sender(
            platform_user_id=user_id,
            open_id=identity.get("open_id"),
            union_id=identity.get("union_id"),
            sender_type=sender_type,
        ),
        message_id=mid,
        mentions_bot=mentioned,
        parts=parts,
        reply_context={
            **context,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "message_id": mid,
            "parent_id": message.get("parent_id"),
            "root_id": message.get("root_id"),
            "create_time": message.get("create_time"),
        },
        raw=raw,
    )


def normalize_event(raw: dict[str, Any], **kwargs: Any) -> InboundMessage | None:
    """无效平台载荷不进入任务；这里只捕捉解析错误，不捕捉持久化错误。"""
    try:
        if len(json.dumps(raw, ensure_ascii=False).encode()) > 1024 * 1024:
            return None
        message = _normalize_event(raw, **kwargs)
        if message is not None:
            # 回调 token 只属于传输认证，持久审计不保留可复用凭据。
            safe_header = {k: v for k, v in (raw.get("header") or {}).items() if k != "token"}
            safe_event = {k: v for k, v in (raw.get("event") or {}).items() if k != "token"}
            message.raw = {**raw, "header": safe_header, "event": safe_event}
        return message
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
