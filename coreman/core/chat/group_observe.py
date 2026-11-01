"""飞书群聊旁听：入站时记账与防抖排队、群消息摘录、静音。

开了旁听的机器人能收到群里所有消息。没 @ 它的消息不建对话任务（群聊的会话键就是 chat_id，
每条都建任务会把正在进行的回复替代掉），只更新这里的状态，再排一个延后的 `group_observe`
任务：群里安静一会儿再判断要不要开口。同一个群连着来很多消息时，只有最后那条的任务会真正
判断，其余的到点发现自己不是最新的就退出（一直有人说话时，最长等 `max_wait` 也会判断一次）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.db.models import (
    Bot,
    FeishuSentMessage,
    GroupObserveState,
    InboundEvent,
    User,
    UserIdentity,
)
from coreman.core.platforms.feishu_content import mention_names, render
from coreman.core.wecom.messages import InboundMessage

# 入站时先按默认安静时长排队，worker 到点再按管理台设置决定是否再等。
FIRST_DELAY_SECONDS = 8
# 判断与插话带的最近群消息：最多这么多条，且只取这段时间内的。
CONTEXT_LIMIT = 20
CONTEXT_WINDOW = timedelta(minutes=30)
TEXT_LIMIT = 1000
MUTE = timedelta(hours=2)
# 插话回合里 agent 判断不该说话时输出的标记；收尾时不发送。
NO_REPLY = "<NO_REPLY>"


def observing(bot: Bot) -> bool:
    return bot.platform == "feishu" and bot.group_observe != "off"


def task_session_key(chat_id: str) -> str:
    """判断任务的会话键：不能和群聊对话共用 chat_id，否则对话的替代会把判断任务取消。"""
    return f"observe:{chat_id}"


async def note(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage, event_id: int
) -> None:
    """一条群消息落库后记账：人说话清零机器人连发计数；没 @ 的消息排一个判断任务。

    @ 机器人的消息照常走对话，清掉待判断：回答完不再主动补一句。
    """
    now = datetime.now(UTC)
    human = message.sender.sender_type != "bot"
    pending = not message.mentions_bot
    values: dict[str, Any] = {
        "bot_id": bot_id,
        "chat_id": message.chat_id,
        "bot_streak": 0 if human else 1,
        "pending_since": now if pending else None,
        "last_event_id": event_id if pending else None,
        "last_message_at": now,
    }
    stmt = insert(GroupObserveState).values(**values)
    table = GroupObserveState.__table__
    stmt = stmt.on_conflict_do_update(
        index_elements=[GroupObserveState.bot_id, GroupObserveState.chat_id],
        set_={
            "bot_streak": 0 if human else table.c.bot_streak + 1,
            "pending_since": func.coalesce(table.c.pending_since, now) if pending else None,
            "last_event_id": event_id if pending else table.c.last_event_id,
            "last_message_at": now,
        },
    )
    await session.execute(stmt)
    if not pending:
        return
    await enqueue(
        session,
        NewTask(
            bot_id=bot_id,
            kind="group_observe",
            lane="fast",
            payload={"chat_id": message.chat_id, "event_id": event_id},
            session_key=task_session_key(message.chat_id),
            inbound_event_id=event_id,
            run_after=now + timedelta(seconds=FIRST_DELAY_SECONDS),
            dedupe_key=f"observe:{event_id}",
        ),
    )


async def set_muted(session: AsyncSession, bot_id: uuid.UUID, chat_id: str, muted: bool) -> None:
    until = datetime.now(UTC) + MUTE if muted else None
    stmt = insert(GroupObserveState).values(bot_id=bot_id, chat_id=chat_id, muted_until=until)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[GroupObserveState.bot_id, GroupObserveState.chat_id],
            set_={"muted_until": until, "pending_since": None},
        )
    )


async def mark_spoke(session: AsyncSession, bot_id: uuid.UUID, chat_id: str) -> None:
    """主动插话发出去了：自己的消息也算一条机器人消息（飞书不会把它推回来）。"""
    await session.execute(
        update(GroupObserveState)
        .where(GroupObserveState.bot_id == bot_id, GroupObserveState.chat_id == chat_id)
        .values(
            bot_streak=GroupObserveState.bot_streak + 1,
            last_interjected_at=datetime.now(UTC),
        )
    )


@dataclass(frozen=True)
class Line:
    """群消息摘录的一行。`event_id` 只有别人发的消息才有，用来定插话回复哪一条。"""

    at: datetime
    sender: str
    sender_kind: str  # human / bot / self
    text: str
    event_id: int | None = None

    def out(self) -> dict[str, str]:
        return {"sender": self.sender, "sender_kind": self.sender_kind, "text": self.text}


async def transcript(
    session: AsyncSession, bot: Bot, chat_id: str, *, now: datetime | None = None
) -> list[Line]:
    """最近的群消息（别人发的入站消息 + 本机器人自己发的），按时间排好。

    发言人用通讯录名字，认不出的按出场顺序叫「同事 N」，其他机器人叫「机器人 N」。
    """
    since = (now or datetime.now(UTC)) - CONTEXT_WINDOW
    inbound = (
        await session.execute(
            select(InboundEvent.id, InboundEvent.received_at, InboundEvent.payload)
            .where(
                InboundEvent.bot_id == bot.id,
                InboundEvent.chat_id == chat_id,
                InboundEvent.kind == "message",
                InboundEvent.received_at >= since,
            )
            .order_by(InboundEvent.id.desc())
            .limit(CONTEXT_LIMIT)
        )
    ).all()
    sent = (
        await session.execute(
            select(FeishuSentMessage.created_at, FeishuSentMessage.text)
            .where(
                FeishuSentMessage.bot_id == bot.id,
                FeishuSentMessage.chat_id == chat_id,
                FeishuSentMessage.created_at >= since,
            )
            .order_by(FeishuSentMessage.created_at.desc())
            .limit(CONTEXT_LIMIT)
        )
    ).all()
    open_ids = {
        str(oid)
        for _, _, payload in inbound
        if (oid := ((payload or {}).get("sender") or {}).get("open_id"))
    }
    names: dict[str, str] = {}
    if open_ids:
        rows = await session.execute(
            select(UserIdentity.open_id, User.display_name)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.platform == "feishu", UserIdentity.open_id.in_(open_ids))
        )
        names = {str(oid): str(name) for oid, name in rows.all() if oid and name}
    aliases: dict[str, str] = {}
    lines: list[Line] = []
    for event_id, received_at, payload in reversed(inbound):
        payload = payload or {}
        sender = payload.get("sender") or {}
        kind = "bot" if sender.get("sender_type") == "bot" else "human"
        key = str(sender.get("open_id") or sender.get("platform_user_id") or event_id)
        if key in names:
            label = names[key]
        else:
            if key not in aliases:
                prefix = "机器人" if kind == "bot" else "同事"
                count = sum(1 for alias in aliases.values() if alias.startswith(prefix))
                aliases[key] = f"{prefix} {count + 1}"
            label = aliases[key]
        text = _message_text(payload)
        if text:
            lines.append(Line(received_at, label, kind, text[:TEXT_LIMIT], int(event_id)))
    for created_at, text in reversed(sent):
        if text:
            lines.append(Line(created_at, bot.name, "self", str(text)[:TEXT_LIMIT]))
    lines.sort(key=lambda line: line.at)
    return lines[-CONTEXT_LIMIT:]


def _message_text(payload: dict[str, Any]) -> str:
    message = ((payload.get("raw") or {}).get("event") or {}).get("message") or {}
    rendered = render(
        str(message.get("message_type") or ""),
        message.get("content"),
        names=mention_names(message.get("mentions")),
    )
    if rendered is not None:
        return rendered.text().strip()
    parts = [p for p in payload.get("parts") or [] if isinstance(p, dict)]
    return "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text").strip()


def transcript_note(lines: list[Line]) -> str:
    """交给 agent 的本轮块：最近群聊记录。只有开了旁听的机器人才有（以前只看得到 @ 它的话）。"""
    if not lines:
        return ""
    body = "\n".join(
        f"- {line.sender}{'（你）' if line.sender_kind == 'self' else ''}"
        f"{'（机器人）' if line.sender_kind == 'bot' else ''}: {line.text}"
        for line in lines
    )
    return "最近的群聊记录（从早到晚，供理解上下文；只回应与你有关的部分）：\n" + body


INTERJECT_NOTE = (
    "这一轮没有人 @ 你：是你看了群聊记录后判断需要你，主动加入讨论。"
    "只说结论或关键信息，一两句话；不寒暄，不复述别人的话，不追问无关细节，"
    "需要展开就等同事追问或 @ 你。"
    f"如果看完上下文发现其实不需要你说话，只输出 {NO_REPLY}，不要输出任何其他内容。"
)
