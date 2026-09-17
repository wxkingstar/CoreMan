"""Bounded, explicit self-reminders; no model parsing or inherited context."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks
from coreman.core.chat import interactions
from coreman.core.chat.commands import classify_command
from coreman.core.chat.identity import resolve_feishu_event_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    CronJob,
    InboundEvent,
    InteractionState,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.errors import ApiError

MODE = "self_reminder"
HELP = (
    "支持本人私聊的一次性提醒，例如“两分钟后提醒我检查接口，只提醒一次”。"
    "请写清分钟、小时或天数；查看或取消请打开[我的提醒](/self-reminders)。"
)
_PATTERN = re.compile(
    r"([0-9]{1,4}|[一二两三四五六七八九十百]{1,5})(分钟|小时|天)后提醒我(.+?)(?:[，,]只提醒一次)?[。！!]?"
)


def parse_request(text: str) -> tuple[int, str] | None:
    match = _PATTERN.fullmatch(text.strip())
    if not match:
        return None
    number, unit, content = match.groups()
    if number.isdigit():
        amount = int(number)
    else:
        if "百" in number and not number.endswith("百") and "十" not in number:
            return None
        if not re.fullmatch(
            r"(?:[一二两三四五六七八九]百)?(?:[二两三四五六七八九]?十)?[一二两三四五六七八九]?",
            number,
        ):
            return None
        digits = dict(zip("一二两三四五六七八九", [1, 2, 2, 3, 4, 5, 6, 7, 8, 9], strict=True))
        amount, pending = 0, 0
        for char in number:
            if char in digits:
                pending = digits[char]
            else:
                amount += (pending or 1) * (10 if char == "十" else 100)
                pending = 0
        amount += pending
    seconds = amount * {"分钟": 60, "小时": 3600, "天": 86400}[unit]
    if not 60 <= seconds <= 30 * 86400 or not 1 <= len(content.strip()) <= 500:
        return None
    if content.startswith(("和", "们", "以及", "、")):
        return None
    if any(
        word in content for word in ("每天", "每周", "每月", "重复", "每隔", "然后提醒", "再提醒")
    ) or any(c in content for c in "\n\r\x00“”「」『』"):
        return None
    return seconds, content.strip()


async def require_actor(session: AsyncSession, bot: Bot, actor: User) -> None:
    allowed = (
        await session.scalars(select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id))
    ).all()
    if (
        not bot.enabled
        or bot.platform != "feishu"
        or actor.status != "active"
        or actor.source == "bootstrap"
        or (allowed and actor.id not in allowed)
    ):
        raise ApiError(403, 403, "reminder_actor_unavailable")
    if not await session.scalar(
        select(UserIdentity.id).where(
            UserIdentity.user_id == actor.id, UserIdentity.platform == "feishu"
        )
    ):
        raise ApiError(403, 403, "reminder_actor_unbound")


async def require_fixed(session: AsyncSession, job: CronJob, bot: Bot, actor: User) -> None:
    await require_actor(session, bot, actor)
    reached = await session.get(UserReached, (bot.id, actor.id), populate_existing=True)
    if (
        job.execution_mode != MODE
        or job.bot_id != bot.id
        or job.created_by != actor.id
        or job.schedule_kind != "once"
        or not job.run_at
        or not job.reminder_chat_id
        or job.target_users != [actor.id]
        or job.target_chats
        or job.notify_emails
        or job.notify_webhook
        or job.notify_webhook_url_enc
        or job.system_prompt
        or job.precheck_script
        or job.force_run_at
        or job.force_run_by
        or not 1 <= len(job.prompt) <= 500
        or reached is None
        or reached.platform_chat_id != job.reminder_chat_id
    ):
        raise ApiError(403, 403, "invalid_fixed_reminder")


def _plain_message_parts(message: dict[str, Any]) -> list[dict[str, str]]:
    """Prove only a plain native payload, without dropping rich or ambiguous content."""
    content = json.loads(message.get("content") or "{}")
    if not isinstance(content, dict):
        raise ValueError("plain_message_required")
    if message.get("message_type") == "text":
        text = content.get("text")
        if not isinstance(text, str):
            raise ValueError("plain_message_required")
        # The gateway strips text-message edges; do not otherwise rewrite its text.
        return [{"type": "text", "text": text.strip()}]
    if message.get("message_type") != "post":
        raise ValueError("plain_message_required")
    if "content" not in content and "content_v2" not in content:
        # The gateway chooses a locale; require exactly one to avoid that ambiguity.
        if len(content) != 1:
            raise ValueError("unambiguous_post_required")
        locale, post = next(iter(content.items()))
        if not re.fullmatch(r"[a-z]{2}_[a-z]{2}", locale) or not isinstance(post, dict):
            raise ValueError("plain_post_required")
        content = post
    keys = set(content) & {"content", "content_v2"}
    if len(keys) != 1 or set(content) - {"title", "content", "content_v2"}:
        raise ValueError("plain_post_required")
    if content.get("title", "") != "":
        raise ValueError("plain_post_required")
    lines = content[next(iter(keys))]
    # One paragraph can have adjacent text spans, but never discard line boundaries.
    if not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], list):
        raise ValueError("single_paragraph_required")
    parts = []
    for segment in lines[0]:
        if (
            not isinstance(segment, dict)
            or set(segment) - {"tag", "text", "style"}
            or segment.get("tag") != "text"
            or not isinstance(segment.get("text"), str)
            or segment.get("style", []) != []
        ):
            raise ValueError("plain_post_required")
        parts.append({"type": "text", "text": segment["text"]})
    if not parts:
        raise ValueError("plain_post_required")
    return parts


async def verified_origin(
    session: AsyncSession, task: Task, cipher: Cipher, text: str
) -> tuple[Bot, User, InboundEvent]:
    event = await session.get(InboundEvent, task.inbound_event_id)
    bot = await session.get(Bot, task.bot_id, with_for_update=True, populate_existing=True)
    if (
        not event
        or not bot
        or (
            task.kind != "chat"
            and not (task.kind == "command" and classify_command(text) in ("stop", "reset"))
        )
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at
        or any(k.startswith("collaboration") for k in task.payload)
    ):
        raise ValueError("origin")
    raw = event.payload.get("raw") or {}
    header, source = raw.get("header") or {}, raw.get("event") or {}
    message, sender = source.get("message") or {}, source.get("sender") or {}
    ids = sender.get("sender_id") or {}
    normalized = event.payload.get("sender") or {}
    plain_parts = _plain_message_parts(message)
    credentials = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    if (
        event.bot_id != bot.id
        or event.kind != "message"
        or event.platform != "feishu"
        or event.chat_type != "single"
        or task.session_key != event.chat_id
        or header.get("app_id") != credentials.get("app_id")
        or not credentials.get("app_id")
        or header.get("event_type") != "im.message.receive_v1"
        or not header.get("tenant_key")
        or sender.get("sender_type") != "user"
        or normalized.get("sender_type") != "user"
        or not event.sender_open_id
        or ids.get("open_id") != event.sender_open_id
        or normalized.get("open_id") != event.sender_open_id
        or (ids.get("user_id") or "") != (event.sender_platform_user_id or "")
        or (normalized.get("platform_user_id") or "") != (event.sender_platform_user_id or "")
        or message.get("chat_id") != event.chat_id
        or message.get("message_id") != event.platform_msg_id
        or message.get("chat_type") != "p2p"
        or any(message.get(key) for key in ("parent_id", "root_id", "thread_id"))
        or event.payload.get("parts") != plain_parts
        or text != "".join(part["text"] for part in plain_parts)
        or task.payload.get("message", event.payload) != event.payload
    ):
        raise ValueError("origin")
    speaker = await resolve_feishu_event_speaker(session, bot=bot, event=event, cipher=cipher)
    actor = (
        await session.get(User, speaker.user_id, with_for_update=True, populate_existing=True)
        if speaker.user_id
        else None
    )
    if actor is None or (task.user_id is not None and task.user_id != actor.id):
        raise ValueError("actor")
    await require_actor(session, bot, actor)
    return bot, actor, event


def displayed(at: datetime) -> str:
    return at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S（北京时间 UTC+08:00）"
    )


async def handle_request(
    session: AsyncSession, task: Task, cipher: Cipher, *, now: datetime | None = None
) -> str | None:
    now = now or datetime.now(UTC)
    event = await session.get(InboundEvent, task.inbound_event_id)
    if not event:
        return None
    parts = event.payload.get("parts") or []
    text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
    )
    command = classify_command(text)
    if "提醒" not in text and command not in ("stop", "reset"):
        return None
    if event.chat_type != "single":
        return "请在机器人私聊中设置本人提醒。" if "提醒" in text else None
    try:
        bot, actor, event = await verified_origin(session, task, cipher, text)
    except (ValueError, TypeError, AttributeError, ApiError):
        return "无法验证本人直接私聊请求，未设置提醒。" if "提醒" in text else None
    # Strip only outer whitespace after the exact normalized parts have been proved.
    text = text.strip()
    scope = f"{bot.id}:{actor.id}:{event.chat_id}"
    pending = await session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == MODE, InteractionState.scope_key == scope)
        .with_for_update()
    )
    if command in ("stop", "reset"):
        if pending and pending.status == "open":
            pending.status = "cancelled"
        return None
    if text in ("确认提醒", "取消提醒"):
        if (
            not pending
            or pending.status != "open"
            or not pending.expires_at
            or pending.expires_at <= now
        ):
            return "没有待确认的有效提醒，请重新发送提醒请求。"
        if text == "取消提醒":
            pending.status = "cancelled"
            return "已取消本次待确认提醒。"
        run_at = datetime.fromisoformat(pending.state["run_at"])
        if run_at <= now:
            pending.status = "expired"
            return "提醒时间已过，请重新发送提醒请求。"
        count = await session.scalar(
            select(func.count())
            .select_from(CronJob)
            .where(
                CronJob.created_by == actor.id,
                CronJob.execution_mode == MODE,
                CronJob.enabled.is_(True),
            )
        )
        if count and count >= 20:
            return "最多可保留 20 个未完成提醒，请先在[我的提醒](/self-reminders)取消部分提醒。"
        # Only a verified current private event may establish this exact reachability.
        await session.execute(
            insert(UserReached)
            .values(bot_id=bot.id, user_id=actor.id, platform_chat_id=event.chat_id)
            .on_conflict_do_update(
                index_elements=["bot_id", "user_id"], set_={"platform_chat_id": event.chat_id}
            )
        )
        session.add(
            CronJob(
                bot_id=bot.id,
                created_by=actor.id,
                name="本人提醒",
                execution_mode=MODE,
                reminder_chat_id=event.chat_id,
                schedule_kind="once",
                cron_expression="",
                run_at=run_at,
                next_run_at=run_at,
                prompt=pending.state["text"],
                target_users=[actor.id],
            )
        )
        pending.status = "submitted"
        await session.flush()
        return (
            f"已设置一次性本人提醒：{displayed(run_at)}\n{pending.state['text']}"
            "\n[查看或取消](/self-reminders)"
        )
    proposal = parse_request(text)
    if not proposal:
        return HELP
    pending_count = await session.scalar(
        select(func.count())
        .select_from(InteractionState)
        .where(
            InteractionState.kind == MODE,
            InteractionState.state["user_id"].astext == str(actor.id),
            InteractionState.status == "open",
            InteractionState.expires_at > now,
            InteractionState.scope_key != scope,
        )
    )
    if pending_count and pending_count >= 20:
        return "最多可保留 20 个待确认提醒，请先确认、取消或等待过期。"
    seconds, content = proposal
    run_at = now + timedelta(seconds=seconds)
    await interactions.open_state(
        session,
        bot_id=bot.id,
        kind=MODE,
        scope_key=scope,
        state={"run_at": run_at.isoformat(), "text": content, "user_id": str(actor.id)},
        expires_at=now + timedelta(minutes=5),
    )
    return (
        f"将在 {displayed(run_at)} 仅向你当前私聊提醒一次：\n{content}"
        "\n请在 5 分钟内精确回复“确认提醒”或“取消提醒”。"
    )
