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
from coreman.core.chat.commands import GATEWAY_COMMANDS, classify_command
from coreman.core.db.models import InboundEvent, Task
from coreman.core.logging import get_logger
from coreman.core.wecom.messages import InboundMessage, text_of

__all__ = ["InboundBot", "InboundMessage", "enqueue_inbound"]

log = get_logger(__name__)


class InboundBot(Protocol):
    """入队只需要 bot 的这几个字段（企微 runner / 飞书子进程 / 开发注入各有各的快照）。"""

    id: uuid.UUID
    bot_key: str
    welcome_message: str | None


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
        new = NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": payload, **base},
            session_key=message.chat_id,
            inbound_event_id=event_id,
            dedupe_key=f"inbound:{event_id}",
        )
    return await enqueue(session, new)
