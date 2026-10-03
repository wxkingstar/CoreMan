"""索取、提交、续接与过期。飞书卡片与 H5 页面两条提交路径汇入同一个 submit()。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    CredentialRequest,
    PlatformApp,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.personal_credentials import cards, policy
from coreman.core.personal_credentials.policy import Capability, CredentialError
from coreman.core.timeutils import utcnow

PLATFORMS = ("feishu", "wecom")
PAGE_PATH = "/my-credentials/requests/"
RESUME_KIND = "credential_resume"
RETENTION = timedelta(days=90)
AGENT_NOTE = (
    "已向用户发送安全表单。请简短告诉用户去填写，然后结束本轮；"
    "用户提交后会自动续接，届时变量已在环境里。"
)


def page_url(base_url: str, request_id: uuid.UUID) -> str:
    return base_url.rstrip("/") + PAGE_PATH + str(request_id)


async def login_available(session: AsyncSession, platform: str) -> bool:
    found = await session.scalar(
        select(PlatformApp.id)
        .where(
            PlatformApp.platform == platform,
            PlatformApp.enabled.is_(True),
            PlatformApp.capabilities.any("login"),  # type: ignore[arg-type]
        )
        .limit(1)
    )
    return found is not None


async def _owner(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Bot, User]:
    bot = await session.get(Bot, bot_id, populate_existing=True)
    user = await session.get(User, user_id, populate_existing=True)
    if bot is None or not bot.enabled or bot.platform not in PLATFORMS:
        raise CredentialError("inactive", "AI 员工已停用")
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise CredentialError("inactive", "账号不可用")
    return bot, user


async def _identity(
    session: AsyncSession, user_id: uuid.UUID, platform: str
) -> UserIdentity | None:
    found = await session.scalars(
        select(UserIdentity)
        .where(UserIdentity.user_id == user_id, UserIdentity.platform == platform)
        .limit(1)
    )
    return found.first()


async def capability_scope(session: AsyncSession, cap: Capability) -> tuple[Bot, User]:
    """令牌只在签发它的那一轮仍在运行时有效。"""
    task = await session.get(Task, cap.task_id, populate_existing=True)
    if (
        task is None
        or task.bot_id != cap.bot_id
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at is not None
    ):
        raise CredentialError("inactive", "这一轮已经结束，令牌失效")
    return await _owner(session, cap.bot_id, cap.user_id)


@dataclass(frozen=True)
class Opened:
    status: Literal["form_sent", "already_pending"]
    request_id: uuid.UUID


def _delivery(cap: Capability, reached: UserReached | None) -> tuple[str | None, bool]:
    """表单发到哪：有私聊记录发私聊；否则对话发回原会话；定时任务没有私聊记录就发不了。"""
    if reached is not None:
        return reached.platform_chat_id, True
    if cap.origin_kind == "chat":
        return cap.chat_id, False
    return None, False


async def open_request(
    session: AsyncSession,
    cipher: Cipher,
    cap: Capability,
    body: Any,
    *,
    base_url: str,
    now: datetime | None = None,
) -> Opened:
    bot, user = await capability_scope(session, cap)
    parsed = policy.parse_request(body)
    now = now or utcnow()
    keys = sorted(f.key for f in parsed.fields)
    pending = await session.scalars(
        select(CredentialRequest).where(
            CredentialRequest.bot_id == bot.id,
            CredentialRequest.user_id == user.id,
            CredentialRequest.status == "open",
            CredentialRequest.expires_at > now,
        )
    )
    for existing in pending:
        if sorted(str(f["key"]) for f in existing.fields) == keys:
            return Opened("already_pending", existing.id)
    reached = await session.get(UserReached, (bot.id, user.id), populate_existing=True)
    delivery, private = _delivery(cap, reached)
    if delivery is None:
        raise CredentialError(
            "unreachable", "没有可以发送表单的会话：请先在私聊里和 AI 员工说一句话，再重新发起"
        )
    if bot.platform == "wecom" and not await login_available(session, "wecom"):
        raise CredentialError(
            "login_unavailable", "企业微信网页登录未配置，无法发送安全表单，请联系管理员"
        )
    identity = await _identity(session, user.id, bot.platform)
    row = CredentialRequest(
        bot_id=bot.id,
        user_id=user.id,
        origin_kind=cap.origin_kind,
        origin_task_id=cap.task_id,
        origin_event_id=cap.event_id,
        origin_chat_id=cap.chat_id,
        origin_chat_type=cap.chat_type,
        origin_session_key=cap.session_key,
        cron_job_id=cap.cron_job_id,
        delivery_chat_id=delivery,
        fields=[f.model_dump() for f in parsed.fields],
        purpose=parsed.purpose,
        status="open",
        expires_at=now + policy.REQUEST_TTL,
    )
    session.add(row)
    await session.flush()
    target: dict[str, Any] = {"chat_id": delivery}
    if private and identity is not None:
        target |= {
            "recipient_user_id": str(user.id),
            "recipient_platform_user_id": identity.platform_user_id,
        }
    url = page_url(base_url, row.id)
    payload: dict[str, Any]
    if bot.platform == "feishu":
        mention = (
            identity.open_id
            if identity is not None and not private and cap.chat_type == "group"
            else None
        )
        payload = {
            "card": cards.form_card(
                row.id,
                bot_name=bot.name,
                purpose=parsed.purpose,
                fields=row.fields,
                web_url=url if await login_available(session, "feishu") else None,
                mention_open_id=mention,
            )
        }
    else:
        payload = {
            "markdown": cards.wecom_link(
                bot_name=bot.name, purpose=parsed.purpose, fields=row.fields, url=url
            )
        }
    item = await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"credential-request:{row.id}:form",
        target=target,
        payload=payload,
    )
    row.request_outbox_id = item.id if item else None
    if cap.origin_kind == "chat" and delivery != cap.chat_id:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=f"credential-request:{row.id}:notice",
            target={"chat_id": cap.chat_id},
            payload={"markdown": cards.GROUP_NOTICE},
        )
    return Opened("form_sent", row.id)
