"""企微入站帧 → `InboundMessage` → `inbound_events` + `tasks`（spec §7.1、§7.3）。

两件事严格分开：`normalize_frame` 是纯函数（不碰库、不看时钟），`enqueue_inbound` 只做落库与
入队。去重靠 `inbound_events (bot_id, platform_msg_id)` 唯一键——企微会重推，重推的那一条
连任务都不建。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.chat.commands import GATEWAY_COMMANDS, classify_command
from coreman.core.chat.identity import looks_like_open_userid
from coreman.core.db.models import InboundEvent, Task
from coreman.core.logging import get_logger
from coreman.core.wecom.messages import (
    AudioPart,
    FilePart,
    ImagePart,
    InboundMessage,
    Part,
    QuotePart,
    Sender,
    TextPart,
    VideoPart,
    parse_card_event,
    text_of,
)

if TYPE_CHECKING:  # BotInfo 定义在 runner 里，runner 又要用本模块的入队函数
    from coreman.runtime.gateway_wecom.runner import BotInfo


class InboundBot(Protocol):
    id: uuid.UUID
    bot_key: str
    welcome_message: str | None


PLATFORM = "wecom"
CHAT_TYPES = ("single", "group")
# 事件类型 → 内部 kind；card_action 建 fast 任务；feedback 只落事件并记日志。
EVENT_KINDS = {
    "enter_chat": "enter_chat",
    "template_card_event": "card_action",
    "feedback_event": "feedback",
}
log = get_logger(__name__)


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str | int) and str(value) else None


def _obj(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    return dict(value) if isinstance(value, dict) else {}


# ---- 归一化 ---------------------------------------------------------------


def normalize_frame(
    bot: BotInfo, frame: dict[str, Any], *, gateway_instance: str, now: datetime
) -> InboundMessage | None:
    """把一帧企微回调转成内部模型；认不出来的帧返回 None 并记 WARNING。"""
    cmd = str(frame.get("cmd") or "")
    body = _obj(frame, "body")
    reply_context = {
        "gateway_instance": gateway_instance,
        "req_id": str(_obj(frame, "headers").get("req_id") or ""),
        "received_at": now.isoformat(),
    }
    if cmd == "aibot_msg_callback":
        return _message(bot, frame, body, reply_context)
    if cmd == "aibot_event_callback":
        return _event(bot, frame, body, reply_context)
    log.warning("wecom_frame_ignored", bot_key=bot.bot_key, cmd=cmd)
    return None


def _message(
    bot: BotInfo, frame: dict[str, Any], body: dict[str, Any], reply_context: dict[str, Any]
) -> InboundMessage:
    chat_type, chat_id, sender = _chat(bot, body)
    reply_context = _with_chat(reply_context, chat_type, chat_id)
    parts = _item_parts(bot, body)
    quote = _obj(body, "quote")
    if quote:
        parts.append(_quote_part(quote))
    return InboundMessage(
        platform=PLATFORM,
        bot_id=bot.id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender=sender,
        message_id=_message_id(bot, body),
        # 企微群里只有 @ 了机器人才会回调，所以群消息一律视为提到了机器人。
        mentions_bot=chat_type == "group",
        parts=parts,
        reply_context=reply_context,
        raw=frame,
    )


def _event(
    bot: BotInfo, frame: dict[str, Any], body: dict[str, Any], reply_context: dict[str, Any]
) -> InboundMessage | None:
    event = _obj(body, "event")
    eventtype = str(event.get("eventtype") or "")
    kind = EVENT_KINDS.get(eventtype)
    if kind is None:
        log.warning("wecom_event_ignored", bot_key=bot.bot_key, eventtype=eventtype)
        return None
    card_action = None
    if kind == "card_action":
        # 没有 task_id 的卡片回调认不出是哪一张卡的哪一次点击，建了任务也只能原地失败。
        action = parse_card_event(event)
        if action is None:
            log.warning("wecom_card_event_invalid", bot_key=bot.bot_key)
            return None
        card_action = action.model_dump()
    chat_type, chat_id, sender = _chat(bot, body)
    reply_context = _with_chat(reply_context, chat_type, chat_id)
    return InboundMessage(
        platform=PLATFORM,
        bot_id=bot.id,
        kind=kind,
        chat_type=chat_type,
        chat_id=chat_id,
        sender=sender,
        message_id=_message_id(bot, body),
        mentions_bot=False,
        parts=[],
        card_action=card_action,
        reply_context=reply_context,
        raw=frame,
    )


def _with_chat(reply_context: dict[str, Any], chat_type: str, chat_id: str) -> dict[str, Any]:
    """`reply_context` 里补上会话坐标。

    `task_streams.reply_context` 是网关收尾时唯一能拿到的上下文：流被企微判失效（846606）
    之后，终稿只能改走 `outbox` 主动推送，而那需要 `chat_id`。少了它，网关就只能回表去猜。
    """
    return {**reply_context, "chat_type": chat_type, "chat_id": chat_id}


def _chat(bot: BotInfo, body: dict[str, Any]) -> tuple[str, str, Sender]:
    """会话归属：群聊 = chatid，单聊 = 发送者 userid；chattype 非法按单聊处理。"""
    sender_raw = _obj(body, "from")
    user_id = str(sender_raw.get("userid") or "")
    chat_type = str(body.get("chattype") or "single")
    if chat_type not in CHAT_TYPES:
        log.warning("wecom_chattype_unknown", bot_key=bot.bot_key, chattype=chat_type)
        chat_type = "single"
    chat_id = str(body.get("chatid") or "") if chat_type == "group" else user_id
    sender = Sender(
        platform_user_id=user_id,
        # 智能机器人回调里 userid 本身常常就是密文 open_userid：照记一份，
        # `inbound_events.sender_open_id` 才留得下审计线索。
        open_id=_opt_str(sender_raw.get("open_userid"))
        or (user_id if looks_like_open_userid(user_id) else None),
        display_name=_opt_str(sender_raw.get("name")),
    )
    return chat_type, chat_id, sender


def _message_id(bot: BotInfo, body: dict[str, Any]) -> str:
    msgid = str(body.get("msgid") or "")
    if msgid:
        return msgid
    # 没有 msgid 就没法去重，但丢事件比重复处理更糟：补一个本地 id，照常入库。
    generated = f"local-{uuid.uuid4().hex}"
    log.warning("wecom_msgid_missing", bot_key=bot.bot_key, generated=generated)
    return generated


def _item_parts(bot: BotInfo, item: dict[str, Any]) -> list[Part]:
    """一个消息体（或 mixed 的一项）转成 parts。媒体只留平台引用，网关不下载。"""
    msgtype = str(item.get("msgtype") or "")
    payload = _obj(item, msgtype)
    if msgtype == "text":
        return [TextPart(text=str(payload.get("content") or ""))]
    if msgtype == "image":
        return [ImagePart(ref=payload)]
    if msgtype == "file":
        return [FilePart(ref=payload, filename=_opt_str(payload.get("filename")))]
    if msgtype == "voice":
        # 企微已经转好文字：content 就是转写，ref 留给可能存在的原始音频引用。
        return [
            AudioPart(
                ref={k: v for k, v in payload.items() if k != "content"},
                transcript=_opt_str(payload.get("content")),
            )
        ]
    if msgtype == "video":
        # 视频只留引用：本期不下载、不转发给模型，worker 会回一句「暂不支持」。
        return [VideoPart(ref=payload)]
    if msgtype == "mixed":
        out: list[Part] = []
        for sub in payload.get("msg_item") or []:
            if isinstance(sub, dict):
                out.extend(_item_parts(bot, sub))
        return out
    log.warning("wecom_msgtype_unsupported", bot_key=bot.bot_key, msgtype=msgtype)
    return []


def _quote_part(quote: dict[str, Any]) -> QuotePart:
    kind = str(quote.get("msgtype") or "text")
    inner = _obj(quote, kind)
    text = _opt_str(inner.get("content")) if kind in ("text", "voice") else None
    return QuotePart(kind=kind, text=text, refs=[inner] if inner and text is None else [])


# ---- 落库与入队 -----------------------------------------------------------


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
        # 卡片点击要在 5 秒内更新卡片，所以走快车道，不排在长对话后面。
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
        # 点赞/点踩本期只留一条日志：没有任务要建，但运营要能数出来。
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
