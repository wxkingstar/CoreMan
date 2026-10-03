"""索取、提交、续接与过期。飞书卡片与 H5 页面两条提交路径汇入同一个 submit()。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    ChatSession,
    CredentialRequest,
    InboundEvent,
    OutboxItem,
    PlatformApp,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.personal_credentials import cards, policy, store
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
AGENT_NOTE_CRON = (
    "已向用户发送安全表单。定时任务不会因为用户提交而续接：请在输出里说明本次缺少凭证，"
    "然后结束本轮；用户提交的值从下一次定时运行起生效。"
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


def _delivery(
    cap: Capability, reached: UserReached | None, identity: UserIdentity | None
) -> tuple[str | None, UserIdentity | None]:
    """表单发到哪，以及私聊收件人。

    私聊记录要配上本人的平台身份才算私聊送达（发送前凭它复核收件人）；否则对话发回原会话；
    定时任务没有私聊送达就发不了。第二项非空即私聊。
    """
    if reached is not None and identity is not None:
        return reached.platform_chat_id, identity
    if cap.origin_kind == "chat":
        return cap.chat_id, None
    return None, None


def _target(chat_id: str, user_id: uuid.UUID, recipient: UserIdentity | None) -> dict[str, Any]:
    """出站目标；私聊带上收件人，网关发送前凭它复核。"""
    target: dict[str, Any] = {"chat_id": chat_id}
    if recipient is not None:
        target |= {
            "recipient_user_id": str(user_id),
            "recipient_platform_user_id": recipient.platform_user_id,
        }
    return target


def _same_origin(row: CredentialRequest, cap: Capability) -> bool:
    """只有同一个对话（会话键和 relay 会话都相同）、或同一个定时任务，才能复用已发出的表单。

    续接要回到各自的来源；/reset 之后的新对话不能接手重置前发出的表单。
    """
    if cap.origin_kind == "cron":
        return row.origin_kind == "cron" and row.cron_job_id == cap.cron_job_id
    return (
        row.origin_kind == "chat"
        and row.origin_session_key == cap.session_key
        and row.origin_relay_session_id == cap.relay_session_id
    )


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
        if sorted(str(f["key"]) for f in existing.fields) == keys and _same_origin(existing, cap):
            return Opened("already_pending", existing.id)
    reached = await session.get(UserReached, (bot.id, user.id), populate_existing=True)
    identity = await _identity(session, user.id, bot.platform)
    delivery, recipient = _delivery(cap, reached, identity)
    if delivery is None:
        raise CredentialError(
            "unreachable", "没有可以发送表单的会话：请先在私聊里和 AI 员工说一句话，再重新发起"
        )
    if bot.platform == "wecom" and not await login_available(session, "wecom"):
        raise CredentialError(
            "login_unavailable", "企业微信网页登录未配置，无法发送安全表单，请联系管理员"
        )
    row = CredentialRequest(
        bot_id=bot.id,
        user_id=user.id,
        origin_kind=cap.origin_kind,
        origin_task_id=cap.task_id,
        origin_event_id=cap.event_id,
        origin_chat_id=cap.chat_id,
        origin_chat_type=cap.chat_type,
        origin_session_key=cap.session_key,
        origin_relay_session_id=cap.relay_session_id,
        cron_job_id=cap.cron_job_id,
        delivery_chat_id=delivery,
        fields=[f.model_dump() for f in parsed.fields],
        purpose=parsed.purpose,
        status="open",
        expires_at=now + policy.REQUEST_TTL,
    )
    session.add(row)
    await session.flush()
    target = _target(delivery, user.id, recipient)
    url = page_url(base_url, row.id)
    payload: dict[str, Any]
    if bot.platform == "feishu":
        mention = (
            identity.open_id
            if identity is not None and recipient is None and cap.chat_type == "group"
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


@dataclass(frozen=True)
class Submitted:
    status: Literal["saved", "duplicate", "expired", "invalid"]
    message: str = ""
    keys: tuple[str, ...] = ()


async def submit(
    session: AsyncSession,
    cipher: Cipher,
    request_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    values: Any,
    limit: int = policy.MAX_VALUE,
    now: datetime | None = None,
) -> Submitted:
    now = now or utcnow()
    row = await session.get(
        CredentialRequest, request_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        raise CredentialError("not_found", "表单不存在")
    if row.user_id != actor_id:
        raise CredentialError("forbidden", "只有发起人本人可以提交")
    if row.status == "submitted":
        return Submitted("duplicate", "已经提交过了，无需重复提交")
    if row.status != "open":
        return Submitted("expired", "表单已失效，请让 AI 员工重新发起")
    if row.expires_at <= now:
        await _close(session, row, "expired")
        return Submitted("expired", "表单已过期，请让 AI 员工重新发起")
    bot, user = await _owner(session, row.bot_id, row.user_id)
    try:
        cleaned = policy.clean_values(row.fields, values, limit=limit)
    except CredentialError as exc:
        return Submitted("invalid", exc.message)
    keys = await store.save(
        session, cipher, bot_id=bot.id, user_id=user.id, fields=row.fields, values=cleaned
    )
    await record_audit(
        session,
        action="personal_credential.saved",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff={"keys": keys},
    )
    tail = await _settle(session, bot, user, row, keys, now)
    # 别的对话或定时任务也在等这些键：值已经保存，它们的表单不必再填，各自续接。
    for waiting in await _waiting(session, row, keys, now):
        await _settle(session, bot, user, waiting, keys, now)
    return Submitted("saved", tail, tuple(keys))


async def _waiting(
    session: AsyncSession, row: CredentialRequest, keys: list[str], now: datetime
) -> list[CredentialRequest]:
    """同一用户、同一 AI 员工下，键全部被刚保存的值覆盖的其他未过期请求。"""
    found = await session.scalars(
        select(CredentialRequest)
        .where(
            CredentialRequest.bot_id == row.bot_id,
            CredentialRequest.user_id == row.user_id,
            CredentialRequest.id != row.id,
            CredentialRequest.status == "open",
            CredentialRequest.expires_at > now,
        )
        .order_by(CredentialRequest.created_at)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return [r for r in found if {str(f["key"]) for f in r.fields} <= set(keys)]


async def _settle(
    session: AsyncSession,
    bot: Bot,
    user: User,
    row: CredentialRequest,
    keys: list[str],
    now: datetime,
) -> str:
    """请求已被满足：标记已提交、续接来源对话、把卡片换成结果；返回结果卡与通知的结尾文字。"""
    row.status, row.submitted_at, row.updated_at = "submitted", now, now
    resumed = await _resume(session, row, bot, user)
    if resumed:
        tail = "AI 员工会继续之前的任务。"
    else:
        tail = "下次执行时生效。" if row.origin_kind == "cron" else "下次对话时生效。"
    await _update_card(session, bot, row, cards.saved_card(row.id, keys, tail))
    if bot.platform == "wecom" and not resumed:
        await _say(session, bot, row, "saved", f"已保存 {'、'.join(keys)}，{tail}")
    return tail


async def _resume(session: AsyncSession, row: CredentialRequest, bot: Bot, user: User) -> bool:
    if row.origin_kind != "chat" or not row.origin_session_key or row.origin_event_id is None:
        return False
    if await session.get(InboundEvent, row.origin_event_id) is None:
        return False
    # 提问之后对话被重置或切走：续接会落进另一个对话，不排任务，卡片也就不承诺「继续」。
    current = await session.get(
        ChatSession, (bot.id, row.origin_session_key), populate_existing=True
    )
    if current is None or current.relay_session_id != row.origin_relay_session_id:
        return False
    identity = await _identity(session, user.id, bot.platform)
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=RESUME_KIND,
            user_id=user.id,
            session_key=row.origin_session_key,
            inbound_event_id=row.origin_event_id,
            dedupe_key=f"credential-request:{row.id}:resume",
            payload={
                "credential_request_id": str(row.id),
                # 来源那一轮可能还没结束：同一会话串行，等它收尾再续接。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": identity.platform_user_id if identity else "",
            },
        ),
    )
    if task is None:
        return False
    row.resume_task_id = task.id
    return True


async def _update_card(
    session: AsyncSession, bot: Bot, row: CredentialRequest, card: dict[str, Any]
) -> None:
    """飞书表单卡换成结果卡；没发出去（没有消息 ID）就不动。"""
    if bot.platform != "feishu" or row.request_outbox_id is None:
        return
    item = await session.get(OutboxItem, row.request_outbox_id)
    mid = (item.payload or {}).get("_feishu_message_id") if item else None
    if item is None or not mid:
        return
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"credential-request:{row.id}:card:{row.status}",
        target={
            "message_id": mid,
            "chat_id": item.target.get("chat_id"),
            "task_id": policy.card_task_id(row.id),
        },
        payload={"card": card},
    )


async def _say(
    session: AsyncSession, bot: Bot, row: CredentialRequest, tag: str, markdown: str
) -> None:
    if not row.delivery_chat_id:
        return
    # 送达会话就是本人私聊时，和发表单一样带上收件人，网关发送前复核。
    reached = await session.get(UserReached, (bot.id, row.user_id), populate_existing=True)
    identity = await _identity(session, row.user_id, bot.platform)
    private = (
        identity
        if reached is not None and reached.platform_chat_id == row.delivery_chat_id
        else None
    )
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"credential-request:{row.id}:{tag}",
        target=_target(row.delivery_chat_id, row.user_id, private),
        payload={"markdown": markdown},
    )


async def _close(
    session: AsyncSession, row: CredentialRequest, status: str, card: dict[str, Any] | None = None
) -> None:
    row.status, row.updated_at = status, utcnow()
    bot = await session.get(Bot, row.bot_id)
    if bot is not None:
        await _update_card(session, bot, row, card or cards.expired_card(row.id))


async def cancel(session: AsyncSession, request_id: uuid.UUID, message: str) -> None:
    row = await session.get(
        CredentialRequest, request_id, with_for_update=True, populate_existing=True
    )
    if row is not None and row.status == "open":
        await _close(session, row, "cancelled", cards.failed_card(row.id, message))


async def expire_due(session: AsyncSession, now: datetime) -> int:
    rows = (
        await session.scalars(
            select(CredentialRequest)
            .where(CredentialRequest.status == "open", CredentialRequest.expires_at < now)
            .order_by(CredentialRequest.expires_at)
            .limit(200)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        await _close(session, row, "expired")
    return len(rows)


async def cleanup(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    doomed = (
        select(CredentialRequest.id)
        .where(CredentialRequest.status != "open", CredentialRequest.created_at < now - RETENTION)
        .order_by(CredentialRequest.created_at)
        .limit(limit)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(CredentialRequest)
        .where(CredentialRequest.id.in_(doomed))
        .returning(CredentialRequest.id)
    )
    return len(result.all())
