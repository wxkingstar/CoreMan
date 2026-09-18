"""每次调用都重新核验来源；身份只来自持久化的入站事件或定时任务，绝不取自调用方参数。

两种来源可以用到企业微信个人工具：本人与机器人的已验证私聊；本人创建、以本人身份运行、
结果只发本人私聊的定时任务。企业微信那边机器人代表的是「授权人」，所以还要另外核对授权人
就是本人（见 service.verify）。
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
    InboundEvent,
    Task,
    User,
    UserIdentity,
    UserReached,
    WecomPersonalGrant,
)
from coreman.core.feishu_personal.policy import self_only

AAD = "wecom_personal.task_capability.v1"
PREFIX = "COREMAN_WECOM_PERSONAL_"
# 运行时声明了它，才会把企业微信个人工具作为附加 MCP 挂上。
RUNTIME_CAPABILITY = "wecom_personal_tools_v1"


@dataclass(frozen=True)
class Scope:
    """一位已核验的本人与一个机器人。`event` 只在私聊来源时存在。"""

    task: Task
    bot: Bot
    user_id: uuid.UUID
    # 私聊里这位本人在企业微信回调中的 ID（明文 userid 或企业主体下的密文 userid）。
    sender_ids: frozenset[str]
    # 本人与机器人的私聊：对话本身，或定时任务的投递目标。
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
    # 私聊凭据绑定签发时的会话；定时任务没有。
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


def bot_credentials(cipher: Cipher, bot: Bot) -> tuple[str, str]:
    data = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    bot_id, secret = data.get("bot_id"), data.get("secret")
    if not isinstance(bot_id, str) or not isinstance(secret, str) or not bot_id or not secret:
        raise ValueError("wecom_bot_unavailable")
    return bot_id, secret


def runtime_supported(capabilities: dict[str, Any] | None, provider: str) -> bool:
    capability = (capabilities or {}).get(provider) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True


async def _allowed(session: AsyncSession, bot: Bot | None, user_id: uuid.UUID) -> bool:
    if bot is None or bot.platform != "wecom" or not bot.enabled:
        return False
    allowed = list(
        await session.scalars(select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id))
    )
    return not allowed or user_id in allowed


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
    """核验持久化的私聊来源。任务生命周期由调用方另行检查。"""
    event = await session.get(InboundEvent, task.inbound_event_id)
    if (
        event is None
        or event.bot_id != task.bot_id
        or event.platform != "wecom"
        or event.chat_type != "single"
        or event.kind != "message"
        or task.session_key != event.chat_id
        or not event.sender_platform_user_id
        # 企业微信单聊的会话就是发送者本人。
        or event.chat_id != event.sender_platform_user_id
    ):
        raise ValueError("wecom_private_chat_required")
    raw = event.payload.get("raw") or {}
    body = raw.get("body") if isinstance(raw, dict) else None
    sender = body.get("from") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or not isinstance(sender, dict)
        or raw.get("cmd") != "aibot_msg_callback"
        or body.get("chattype") != "single"
        or sender.get("userid") != event.sender_platform_user_id
    ):
        raise ValueError("verified_private_origin_required")
    speaker = await resolve_speaker(
        session, platform="wecom", platform_user_id=event.sender_platform_user_id
    )
    if not speaker.known or speaker.user_id is None or str(speaker.user_id) != actor:
        raise ValueError("personal_actor_mismatch")
    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    if not await _allowed(session, bot, speaker.user_id):
        raise ValueError("personal_permission_revoked")
    assert bot is not None
    ids = {event.sender_platform_user_id}
    if event.sender_open_id:
        ids.add(event.sender_open_id)
    return Scope(task, bot, speaker.user_id, frozenset(ids), event.chat_id, event)


async def scheduled_scope(session: AsyncSession, task_id: int, actor: str) -> Scope:
    """运行中的定时任务只有仍归本人、只发本人、授权仍在时才能以本人身份调用。

    所有者与执行者必须是同一人（别的管理员强制运行不算本人），投递快照里的每个目标都只能是
    本人私聊。
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
    if (
        user is None
        or user.status != "active"
        or user.source == "bootstrap"
        or not await _allowed(session, bot, user.id)
    ):
        raise ValueError("personal_permission_revoked")
    assert bot is not None
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user.id, UserIdentity.platform == "wecom"
        )
    )
    reached = await session.get(UserReached, (bot.id, user.id), populate_existing=True)
    grant = await session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    if identity is None or reached is None or grant is None or grant.status != "connected":
        raise ValueError("personal_grant_required")
    return Scope(task, bot, user.id, frozenset(), reached.platform_chat_id)


async def capability_scope(session: AsyncSession, task_id: int, actor: str) -> Scope:
    """凭据只指明任务；由任务自己的类型决定要核验哪种来源。"""
    task = await session.get(Task, task_id, populate_existing=True)
    if task is not None and task.kind == "cron_run":
        return await scheduled_scope(session, task_id, actor)
    return await task_scope(session, task_id, actor)
