"""群文档工具的能力凭证与来源核验：每次调用都按任务和原始事件重新判定，不信调用方的说法。

只有飞书群里真人发来的消息（或他点的回复按钮）开启的回合才行；旁听插话、调度回合、
机器人之间求助都不行。提问人能对上内部员工时记下他，B 层放行只认这个人。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat.card_replies import CLICK_EVENT
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent, Task
from coreman.core.feishu_personal.policy import PRIVATE_TURN_KINDS

AAD = "feishu_docs.capability.v1"
PREFIX = "COREMAN_FEISHU_DOCS_"
CAPABILITY_GRACE = 300
RUNTIME_CAPABILITY = "feishu_group_docs_v1"
# 本人发来的一轮，或凭证提交、审批决定后在原会话里续接它的一轮（来源事件相同）。
TURN_KINDS = PRIVATE_TURN_KINDS
# 只答不做的回合不挂。
EXCLUDED_ORIGINS = ("observe", "dispatch")


@dataclass(frozen=True)
class Capability:
    task_id: int
    chat_id: str
    session_id: uuid.UUID


@dataclass(frozen=True)
class DocsScope:
    task: Task
    bot: Bot
    event: InboundEvent
    chat_id: str
    # 提问人对应的内部员工；对不上时只有 A 层。
    speaker_user_id: uuid.UUID | None


def issue_capability(
    cipher: Cipher, *, task_id: int, chat_id: str, session_id: uuid.UUID, ttl_seconds: int
) -> str:
    claims = {
        "task": task_id,
        "chat": chat_id,
        "session": str(session_id),
        "exp": time.time() + ttl_seconds,
    }
    return cipher.encrypt(json.dumps(claims), AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, AAD))
    if not isinstance(data, dict) or data["exp"] <= time.time():
        raise ValueError("expired_capability")
    return Capability(int(data["task"]), str(data["chat"]), uuid.UUID(data["session"]))


def runtime_supported(capabilities: dict[str, Any] | None, provider: str) -> bool:
    capability = (capabilities or {}).get(provider) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True


def _verified_raw(event: InboundEvent) -> None:
    """原始事件与落库的列一致：群消息看发送人与会话，按钮点击看点击人与卡片所在会话。"""
    raw = event.payload.get("raw") or {}
    if not isinstance(raw, dict):
        raise ValueError("verified_group_origin_required")
    header, source = raw.get("header") or {}, raw.get("event") or {}
    if not isinstance(header, dict) or not isinstance(source, dict) or not header.get("app_id"):
        raise ValueError("verified_group_origin_required")
    if header.get("event_type") == CLICK_EVENT:
        operator, context = source.get("operator") or {}, source.get("context") or {}
        if (
            not isinstance(operator, dict)
            or not isinstance(context, dict)
            or context.get("open_chat_id") != event.chat_id
            or operator.get("open_id") != event.sender_open_id
            or operator.get("user_id") != event.sender_platform_user_id
        ):
            raise ValueError("verified_group_origin_required")
        return
    message, sender = source.get("message") or {}, source.get("sender") or {}
    ids = sender.get("sender_id") if isinstance(sender, dict) else None
    if (
        not isinstance(message, dict)
        or not isinstance(ids, dict)
        or sender.get("sender_type") != "user"
        or message.get("chat_type") != "group"
        or message.get("chat_id") != event.chat_id
        or ids.get("open_id") != event.sender_open_id
        or ids.get("user_id") != event.sender_platform_user_id
    ):
        raise ValueError("verified_group_origin_required")


async def scope(session: AsyncSession, cap: Capability) -> DocsScope:
    task = await session.get(Task, cap.task_id, populate_existing=True)
    if (
        task is None
        or task.kind not in TURN_KINDS
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at
        or task.payload.get("collaboration_id")
        or task.payload.get("collaboration_phase")
        or task.payload.get("origin") in EXCLUDED_ORIGINS
        or task.inbound_event_id is None
    ):
        raise ValueError("group_human_task_required")
    event = await session.get(InboundEvent, task.inbound_event_id)
    if (
        event is None
        or event.bot_id != task.bot_id
        or event.platform != "feishu"
        or event.chat_type != "group"
        or event.kind != "message"
        or event.chat_id != cap.chat_id
        or not event.sender_open_id
        or not event.sender_platform_user_id
        or (event.payload.get("sender") or {}).get("sender_type") != "user"
    ):
        raise ValueError("group_human_task_required")
    _verified_raw(event)
    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    if bot is None or bot.platform != "feishu" or not bot.enabled:
        raise ValueError("bot_unavailable")
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=event.sender_platform_user_id
    )
    return DocsScope(task, bot, event, event.chat_id, speaker.user_id if speaker.known else None)
