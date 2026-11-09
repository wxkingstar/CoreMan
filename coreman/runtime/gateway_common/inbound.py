"""`InboundMessage` → `inbound_events` + `tasks`：与平台无关的入站落库与入队。

各平台网关只负责把回调归一化成 `InboundMessage`（企微 `normalize_frame`、飞书
`normalize_event`），落库与建任务都走这里。去重靠 `inbound_events (bot_id, platform_msg_id)`
唯一键——平台会重推，重推的那一条连任务都不建。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.chat.commands import GATEWAY_COMMANDS, classify_command, is_cancel_word
from coreman.core.db.models import InboundEvent, Task
from coreman.core.i18n.messages import msg
from coreman.core.logging import get_logger
from coreman.core.wecom.messages import InboundMessage, text_of

__all__ = ["InboundBot", "InboundMessage", "enqueue_inbound"]

log = get_logger(__name__)


class InboundBot(Protocol):
    """入队只需要 bot 的这几个字段（企微 runner / 飞书子进程 / 开发注入各有各的快照）。"""

    id: uuid.UUID
    bot_key: str
    welcome_message: str | None
    # 飞书子进程的快照另带 `observe`（群聊旁听开关），其余快照没有，按关闭处理。


def _obj(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    return dict(value) if isinstance(value, dict) else {}


async def enqueue_inbound(
    session: AsyncSession, bot: InboundBot, message: InboundMessage, *, lease_generation: int
) -> Task | None:
    """写 `inbound_events`（去重）并按 kind 建任务 / 入欢迎语；重复投递返回 None。

    调用方负责提交；提交之后 `tasks_queued` / `outbox_added` 通知才会发出。
    """
    payload = message.model_dump(mode="json")
    stmt = (
        insert(InboundEvent)
        .values(
            bot_id=bot.id,
            platform=message.platform,
            platform_msg_id=message.message_id,
            kind=message.kind,
            chat_type=message.chat_type,
            chat_id=message.chat_id,
            sender_platform_user_id=message.sender.platform_user_id or None,
            sender_open_id=message.sender.open_id,
            payload=payload,
            reply_context=message.reply_context,
        )
        .on_conflict_do_nothing(index_elements=[InboundEvent.bot_id, InboundEvent.platform_msg_id])
        .returning(InboundEvent.id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        # 平台重推：事件、任务、出站都不能再来一遍。
        log.debug("inbound_duplicate", bot_key=bot.bot_key, platform_msg_id=message.message_id)
        return None
    event_id = int(row[0])
    # 群聊旁听（只有飞书子进程的快照带 observe）：每条群消息都记账；没 @ 的到此为止，
    # 由延后的判断任务决定要不要开口。
    if (
        getattr(bot, "observe", False)
        and message.kind == "message"
        and message.chat_type == "group"
    ):
        from coreman.core.chat import group_observe

        await group_observe.note(session, bot.id, message, event_id)
        from coreman.core.chat import alert_sources

        source = await alert_sources.match(session, bot.id, message)
        if source is not None:
            return await alert_sources.trigger(
                session, bot.bot_key, source, message, payload, event_id
            )
        if not message.mentions_bot:
            return None
    # Bot receipts are retained for collaboration reconciliation, never human intake.
    if message.sender.sender_type == "bot":
        return None
    if message.kind == "message":
        return await _enqueue_task(session, bot, message, payload, event_id)
    if message.kind == "card_action" and message.card_action:
        # 卡片点击要在平台的回调窗口内更新卡片，所以走快车道，不排在长对话后面。
        return await enqueue(
            session,
            NewTask(
                bot_id=bot.id,
                kind="card_action",
                lane="fast",
                payload={
                    "card_action": message.card_action,
                    "bot_key": bot.bot_key,
                    "platform_user_id": message.sender.platform_user_id,
                    "chat_type": message.chat_type,
                    "chat_id": message.chat_id,
                },
                session_key=message.chat_id,
                inbound_event_id=event_id,
                dedupe_key=f"inbound:{event_id}",
            ),
        )
    if message.kind == "feedback":
        # 点赞/点踩本期只留一条日志：没有任务要建，但运营要能数出来（目前只有企微会回调它）。
        fb = _obj(_obj(_obj(message.raw, "body"), "event"), "feedback_event")
        log.info(
            "wecom_feedback",
            bot_key=bot.bot_key,
            feedback_type=fb.get("type"),
            feedback_id=fb.get("id"),
        )
    if message.kind == "enter_chat" and bot.welcome_message:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=message.platform,
            kind="welcome",
            dedupe_key=f"welcome:{message.message_id}",
            target={
                "req_id": message.reply_context.get("req_id", ""),
                "chat_id": message.chat_id,
                "message_id": message.reply_context.get("message_id"),
            },
            payload={"text": bot.welcome_message},
            lease_generation=lease_generation,
        )
    return None


async def _enqueue_task(
    session: AsyncSession,
    bot: InboundBot,
    message: InboundMessage,
    payload: dict[str, Any],
    event_id: int,
) -> Task | None:
    """纯命令走 fast 车道的 command 任务，其余一律 chat。"""
    base: dict[str, Any] = {
        "bot_key": bot.bot_key,
        "platform_user_id": message.sender.platform_user_id,
    }
    command = classify_command(text_of(message))
    if (
        message.platform == "feishu"
        and message.chat_type == "group"
        and message.sender.platform_user_id
        and is_cancel_word(text_of(message))
    ):
        from sqlalchemy import or_, select

        from coreman.core.chat.bot_collaboration import ACTIVE
        from coreman.core.db.models import BotCollaboration, BotCollaborationRoute

        active = await session.scalar(
            select(BotCollaboration.id)
            .join(BotCollaborationRoute)
            .where(
                or_(
                    BotCollaborationRoute.source_bot_id == bot.id,
                    BotCollaborationRoute.target_bot_id == bot.id,
                ),
                BotCollaborationRoute.chat_id == message.chat_id,
                BotCollaboration.origin_platform_user_id == message.sender.platform_user_id,
                BotCollaboration.status.in_(ACTIVE),
            )
            .limit(1)
        )
        if active:
            command = "stop"
    if (
        message.platform == "feishu"
        and message.sender.platform_user_id
        and command is None
        and is_cancel_word(text_of(message))
    ):
        from coreman.core.chat.human_collaboration import active_for_origin

        if await active_for_origin(
            session, bot.id, message.chat_id, message.sender.platform_user_id
        ):
            command = "stop"
    if command in GATEWAY_COMMANDS:
        new = NewTask(
            bot_id=bot.id,
            kind="command",
            lane="fast",
            payload={"command": command, **base},
            session_key=message.chat_id,
            inbound_event_id=event_id,
            dedupe_key=f"inbound:{event_id}",
        )
    else:
        session_key = message.chat_id
        new = NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": payload, **base},
            session_key=session_key,
            inbound_event_id=event_id,
            dedupe_key=f"inbound:{event_id}",
        )
    helper_reply = False
    if message.platform == "feishu" and message.chat_type == "group" and new.kind == "chat":
        from coreman.core.chat.human_collaboration import is_helper_reply

        # A colleague answering a platform ask is feedback, not a new request for this group.
        helper_reply = await is_helper_reply(
            session, bot.id, message.reply_context, message.sender.platform_user_id
        )
    if message.platform == "feishu" and message.chat_type == "group" and not helper_reply:
        from coreman.core.chat.bot_collaboration import admit_human

        admission = await admit_human(
            session,
            bot.id,
            message.chat_id,
            message.sender.platform_user_id,
            command=str(new.payload["command"]) if new.kind == "command" else None,
        )
        if admission is not None:
            new.payload["serialize_session"] = True
            new.payload["interrupted_previous_task"] = admission.interrupted
    if message.chat_type == "group" and new.kind == "chat" and not helper_reply:
        await _queue_behind_others(session, bot, message, new, event_id)
    offer = (
        new.kind == "chat"
        and not helper_reply
        and not new.payload.get("serialize_session")
        and await _queue_behind_own(session, bot, message, new)
    )
    task = await enqueue(session, new)
    if task is not None and offer:
        await _offer_busy_choice(session, bot, message, task.id, event_id)
    return task


async def _queue_behind_own(
    session: AsyncSession, bot: InboundBot, message: InboundMessage, new: NewTask
) -> bool:
    """同一个人上一条还没做完：这条排在后面，不再直接停掉上一条；返回要不要发选择卡片。

    这个人前面已经有一条在等他选的，就只排队不再发卡：连发几条不刷一串卡片。
    """
    from sqlalchemy import select

    sender = message.sender.platform_user_id or ""
    if not sender:
        return False
    held = await session.scalar(
        select(Task)
        .where(
            Task.bot_id == bot.id,
            Task.session_key == new.session_key,
            Task.kind == "chat",
            Task.status == "queued",
            Task.cancel_requested_at.is_(None),
            Task.payload["stale_hold"].astext == "true",
            Task.payload["platform_user_id"].astext == sender,
        )
        .order_by(Task.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if held is not None:
        # 在等确认时又发了新消息：算回答了，和等着的那些合在一起马上处理。
        from coreman.core.bus.tasks import release_hold

        await release_hold(session, held)
        new.payload["serialize_session"] = True
        return False
    own = (
        select(Task.id)
        .where(
            Task.bot_id == bot.id,
            Task.session_key == new.session_key,
            Task.kind == "chat",
            Task.status.in_(("queued", "claimed", "running")),
            Task.cancel_requested_at.is_(None),
            Task.payload["origin"].astext.is_distinct_from("observe"),
            Task.payload["platform_user_id"].astext == sender,
        )
        .limit(1)
    )
    if await session.scalar(own) is None:
        return False
    new.payload["serialize_session"] = True
    waiting = await session.scalar(
        own.where(Task.status == "queued", Task.payload["busy_offer"].astext == "true")
    )
    if waiting is not None:
        return False
    new.payload["busy_offer"] = True
    return True


async def _offer_busy_choice(
    session: AsyncSession, bot: InboundBot, message: InboundMessage, task_id: int, event_id: int
) -> None:
    from coreman.core.chat import busy_choice

    target: dict[str, Any] = {"chat_id": message.chat_id}
    progress = await busy_choice.progress_of(
        session,
        bot.id,
        message.chat_id,
        message.sender.platform_user_id or "",
        before_task_id=task_id,
        min_running=60,
    )
    payload: dict[str, Any] = {
        "card": busy_choice.offer_card(message.platform, task_id, progress=progress)
    }
    message_id = message.reply_context.get("message_id")
    if message.platform == "feishu" and message_id:
        # 引用这条消息发卡：群里一眼看出问的是哪一条。
        target["message_id"] = message_id
        payload["_reply"] = True
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=message.platform,
        kind="send",
        dedupe_key=f"inbound:{event_id}:busy",
        target=target,
        payload=payload,
    )


async def _queue_behind_others(
    session: AsyncSession, bot: InboundBot, message: InboundMessage, new: NewTask, event_id: int
) -> None:
    """群里同一个机器人只有一条会话：别人交代的事还没做完时，这条排在后面，不打断它。

    同一个人接着发的消息见 `_queue_behind_own`；主动插话不算（谁 @ 它都让给谁）。
    飞书上引用这条消息回一句「先做完手上的再处理」，免得对方以为没人理。
    """
    from sqlalchemy import select

    sender = message.sender.platform_user_id or ""
    others = await session.scalar(
        select(Task.id)
        .where(
            Task.bot_id == bot.id,
            Task.session_key == new.session_key,
            Task.kind == "chat",
            Task.status.in_(("queued", "claimed", "running")),
            Task.cancel_requested_at.is_(None),
            Task.payload["origin"].astext.is_distinct_from("observe"),
            Task.payload["platform_user_id"].astext.is_distinct_from(sender),
        )
        .limit(1)
    )
    if others is None:
        return
    new.payload["serialize_session"] = True
    message_id = message.reply_context.get("message_id")
    if message.platform == "feishu" and message_id:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"inbound:{event_id}:queued",
            target={"chat_id": message.chat_id, "message_id": message_id},
            payload={"text": msg("group_queued"), "_reply": True},
        )
