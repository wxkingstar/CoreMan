"""Revalidate the durable platform origin on every call; no caller-supplied identity."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotAllowedUser, InboundEvent, Task

AAD = "feishu_personal.task_capability.v1"
PREFIX = "COREMAN_FEISHU_PERSONAL_"


@dataclass(frozen=True)
class Scope:
    task: Task
    bot: Bot
    user_id: uuid.UUID
    event: InboundEvent
    tenant_key: str
    app_id: str


def issue_capability(
    cipher: Cipher,
    *,
    task_id: int,
    user_id: str,
    context_epoch: uuid.UUID,
    base_session_id: uuid.UUID,
) -> str:
    return cipher.encrypt(
        json.dumps(
            {
                "task": task_id,
                "actor": user_id,
                "exp": time.time() + 1800,
                "epoch": str(context_epoch),
                "session": str(base_session_id),
            }
        ),
        AAD,
    )


def read_capability(cipher: Cipher, token: str) -> tuple[int, str, uuid.UUID, uuid.UUID]:
    data = json.loads(cipher.decrypt(token, AAD))
    if not isinstance(data, dict) or data["exp"] <= time.time():
        raise ValueError("expired_capability")
    return (
        int(data["task"]),
        str(uuid.UUID(data["actor"])),
        uuid.UUID(data["epoch"]),
        uuid.UUID(data["session"]),
    )


def app_credentials(cipher: Cipher, bot: Bot) -> tuple[str, str]:
    data = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    app_id, secret = data.get("app_id"), data.get("app_secret")
    if not isinstance(app_id, str) or not isinstance(secret, str) or not app_id or not secret:
        raise ValueError("feishu_app_unavailable")
    return app_id, secret


async def task_scope(session: AsyncSession, task_id: int, actor: str) -> Scope:
    task = await session.get(Task, task_id, populate_existing=True)
    if (
        task is None
        or task.kind != "chat"
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at
        or task.payload.get("collaboration_id")
        or task.payload.get("collaboration_phase")
    ):
        raise ValueError("private_human_task_required")
    event = await session.get(InboundEvent, task.inbound_event_id)
    if (
        event is None
        or event.bot_id != task.bot_id
        or event.platform != "feishu"
        or event.chat_type != "single"
        or event.kind != "message"
        or task.session_key != event.chat_id
        or not event.sender_open_id
        or not event.sender_platform_user_id
        or (event.payload.get("sender") or {}).get("sender_type") != "user"
    ):
        raise ValueError("feishu_private_chat_required")
    raw = event.payload.get("raw") or {}
    if not isinstance(raw, dict):
        raise ValueError("verified_private_origin_required")
    header, source = raw.get("header") or {}, raw.get("event") or {}
    if not isinstance(header, dict) or not isinstance(source, dict):
        raise ValueError("verified_private_origin_required")
    message, sender = source.get("message") or {}, source.get("sender") or {}
    if not isinstance(message, dict) or not isinstance(sender, dict):
        raise ValueError("verified_private_origin_required")
    ids = sender.get("sender_id") or {}
    if not isinstance(ids, dict):
        raise ValueError("verified_private_origin_required")
    if (
        not header.get("tenant_key")
        or not header.get("app_id")
        or sender.get("sender_type") != "user"
        or message.get("chat_type") != "p2p"
        or message.get("chat_id") != event.chat_id
        or ids.get("open_id") != event.sender_open_id
        or ids.get("user_id") != event.sender_platform_user_id
    ):
        raise ValueError("verified_private_origin_required")
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=event.sender_platform_user_id
    )
    if not speaker.known or str(speaker.user_id) != actor or speaker.user_id is None:
        raise ValueError("personal_actor_mismatch")
    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    allowed = list(
        await session.scalars(
            select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == task.bot_id)
        )
    )
    if (
        bot is None
        or bot.platform != "feishu"
        or not bot.enabled
        or (allowed and speaker.user_id not in allowed)
    ):
        raise ValueError("personal_permission_revoked")
    return Scope(
        task, bot, speaker.user_id, event, str(header["tenant_key"]), str(header["app_id"])
    )
