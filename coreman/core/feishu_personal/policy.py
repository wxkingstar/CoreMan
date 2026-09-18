"""Revalidate the durable origin on every call; no caller-supplied identity.

Two origins may use a personal grant: the owner's verified private chat, and a scheduled job
that the owner created, that runs as the owner and that delivers only to the owner.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    CronJob,
    FeishuPersonalGrant,
    InboundEvent,
    Task,
    User,
    UserIdentity,
    UserReached,
)

AAD = "feishu_personal.task_capability.v1"
PREFIX = "COREMAN_FEISHU_PERSONAL_"
# 运行时声明了它，才会把个人工具作为附加 MCP 挂上，而不改变机器人原有能力。
RUNTIME_CAPABILITY = "feishu_personal_tools_v1"
# `self_reminder` 不调模型；其余模式在归本人所有、只发给本人时可以用本人授权。
PERSONAL_JOB_MODES = ("ai", "personal_ai")
# 还没有授权记录时签给能力凭据的上下文版本；之后任何授权变动都会让它失配。
NO_GRANT_EPOCH = uuid.UUID(int=0)


@dataclass(frozen=True)
class Scope:
    """One verified owner of one bot grant. `event` exists only for private-chat origins."""

    task: Task
    bot: Bot
    user_id: uuid.UUID
    tenant_key: str
    app_id: str
    open_id: str
    platform_user_id: str
    # The owner's private chat with this bot: the chat itself, or where a job delivers.
    chat_id: str
    event: InboundEvent | None = None

    @property
    def scheduled(self) -> bool:
        return self.event is None


@dataclass(frozen=True)
class Capability:
    task_id: int
    actor: str
    epoch: uuid.UUID
    # Chat capabilities are fenced by the chat session they were issued for.
    session_id: uuid.UUID | None


def issue_capability(
    cipher: Cipher,
    *,
    task_id: int,
    user_id: str,
    context_epoch: uuid.UUID,
    base_session_id: uuid.UUID | None,
) -> str:
    claims: dict[str, Any] = {
        "task": task_id,
        "actor": user_id,
        "exp": time.time() + 1800,
        "epoch": str(context_epoch),
    }
    if base_session_id is not None:
        claims["session"] = str(base_session_id)
    return cipher.encrypt(json.dumps(claims), AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, AAD))
    if not isinstance(data, dict) or data["exp"] <= time.time():
        raise ValueError("expired_capability")
    return Capability(
        int(data["task"]),
        str(uuid.UUID(data["actor"])),
        uuid.UUID(data["epoch"]),
        uuid.UUID(data["session"]) if "session" in data else None,
    )


def app_credentials(cipher: Cipher, bot: Bot) -> tuple[str, str]:
    data = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    app_id, secret = data.get("app_id"), data.get("app_secret")
    if not isinstance(app_id, str) or not isinstance(secret, str) or not app_id or not secret:
        raise ValueError("feishu_app_unavailable")
    return app_id, secret


def runtime_supported(capabilities: dict[str, Any] | None, provider: str) -> bool:
    capability = (capabilities or {}).get(provider) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True


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
    return await verified_origin_scope(session, task, actor)


async def verified_origin_scope(session: AsyncSession, task: Task, actor: str) -> Scope:
    """Validate a durable private origin. Callers must separately validate task lifecycle."""
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
        task,
        bot,
        speaker.user_id,
        str(header["tenant_key"]),
        str(header["app_id"]),
        str(event.sender_open_id),
        str(event.sender_platform_user_id),
        event.chat_id,
        event,
    )


def self_only(config: dict[str, Any], owner: uuid.UUID | str) -> bool:
    """A job snapshot whose result can only reach its owner (see cron.delivery)."""
    recipients = config.get("target_users") or []
    return (
        config.get("execution_mode", "ai") in PERSONAL_JOB_MODES
        and isinstance(recipients, list)
        and {str(item) for item in recipients} <= {str(owner)}
        and not config.get("target_chats")
        and not config.get("notify_emails")
        and not config.get("notify_webhook")
    )


async def scheduled_scope(session: AsyncSession, task_id: int, actor: str) -> Scope:
    """A running job may act as its owner only while it stays owned, private and authorized.

    Owner and executor must be the same person (another administrator's forced run is not the
    owner), and every destination in the delivery snapshot must be the owner's private chat.
    """
    task = await session.get(Task, task_id, populate_existing=True)
    if (
        task is None
        or task.kind != "cron_run"
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at
        or str(task.user_id) != actor
    ):
        raise ValueError("scheduled_owner_task_required")
    config = task.payload.get("config") or {}
    try:
        job_id = uuid.UUID(str(task.payload.get("cron_job_id")))
    except ValueError:
        raise ValueError("private_owned_schedule_required") from None
    job = await session.get(CronJob, job_id, populate_existing=True)
    if (
        job is None
        or job.running_task_id != task.id
        or str(job.created_by) != actor
        or job.bot_id != task.bot_id
        or not isinstance(config, dict)
        or not self_only(config, job.created_by)
    ):
        raise ValueError("private_owned_schedule_required")
    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    user = await session.get(User, job.created_by, populate_existing=True)
    allowed = list(
        await session.scalars(
            select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == task.bot_id)
        )
    )
    if (
        bot is None
        or bot.platform != "feishu"
        or not bot.enabled
        or user is None
        or user.status != "active"
        or user.source == "bootstrap"
        or (allowed and user.id not in allowed)
    ):
        raise ValueError("personal_permission_revoked")
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user.id, UserIdentity.platform == "feishu"
        )
    )
    reached = await session.get(UserReached, (bot.id, user.id), populate_existing=True)
    grant = await session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    if (
        identity is None
        or reached is None
        or grant is None
        or grant.platform_user_id != identity.platform_user_id
    ):
        raise ValueError("personal_grant_required")
    return Scope(
        task,
        bot,
        user.id,
        grant.tenant_key,
        grant.app_id,
        grant.open_id,
        grant.platform_user_id,
        reached.platform_chat_id,
    )


async def capability_scope(session: AsyncSession, task_id: int, actor: str) -> Scope:
    """The capability names a task; the task's own kind decides which origin must hold."""
    task = await session.get(Task, task_id, populate_existing=True)
    if task is not None and task.kind == "cron_run":
        return await scheduled_scope(session, task_id, actor)
    return await task_scope(session, task_id, actor)
