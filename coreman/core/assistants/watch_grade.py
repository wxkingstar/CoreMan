"""盯消息的分级：一条拉到的消息该立即交回、攒批、压着，还是忽略。

纯函数，不碰库也不调飞书：消息内容只在调用方的内存里。分级表见 spec 第五节。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from coreman.core.db.models import AssistantSettings
from coreman.core.timeutils import aware_utc

# 所有者在一个会话里亲自发言之后这么久内，这个会话的条目先压着。
HOLD_WINDOW = timedelta(minutes=10)
# 攒批的条目每这么久交回一次。
BATCH_WINDOW = timedelta(minutes=5)


class Level(StrEnum):
    IMMEDIATE = "immediate"
    BATCH = "batch"
    HOLD = "hold"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Item:
    """一件可能要交回的事。`kind` 为 `message` 时其余字段取自飞书的一条消息。"""

    chat_id: str
    chat_kind: str
    message_id: str
    sender_id: str
    sender_type: str
    at_ms: int
    kind: str = "message"
    thread_id: str | None = None
    msg_type: str = "text"
    text: str = ""
    mentions: frozenset[str] = frozenset()
    deleted: bool = False
    chat_name: str = ""

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.at_ms / 1000, UTC)


@dataclass(frozen=True)
class Context:
    """分级时消息之外要知道的：现在几点，所有者上次在这个会话里亲自发言是什么时候。"""

    now: datetime
    owner_spoke_at: datetime | None = None


def _holding(context: Context) -> bool:
    spoke = context.owner_spoke_at
    return spoke is not None and aware_utc(context.now) - aware_utc(spoke) < HOLD_WINDOW


def grade(item: Item, settings: AssistantSettings, context: Context) -> Level:
    """按 spec 第五节的表分级。先排除（忽略），再看压着，最后才分立即与攒批。"""
    if item.kind == "approval_outcome":
        return Level.IMMEDIATE
    if item.kind == "approval_todo":
        return Level.BATCH
    owner = settings.open_id
    if item.chat_id in (settings.ignored_chats_enc or []):
        return Level.IGNORE
    if item.deleted or item.msg_type == "system":
        return Level.IGNORE
    human = item.sender_type == "user"
    if human and item.sender_id == owner:
        return Level.IGNORE
    mentioned = owner in item.mentions
    private = item.chat_kind == "p2p"
    if not private and not human and not mentioned:
        # 群里没 @ 他的机器人消息。
        return Level.IGNORE
    if _holding(context):
        return Level.HOLD
    if private:
        # 私聊里机器人发来的（审批待办等通知）不急，攒批。
        return Level.IMMEDIATE if human else Level.BATCH
    if mentioned or item.chat_id in (settings.focus_chats_enc or []):
        return Level.IMMEDIATE
    return Level.BATCH


def release_at(level: Level, context: Context, *, batch_until: datetime | None) -> datetime | None:
    """这一级的条目压到什么时候才交回；立即交回的返回 None。

    Args:
        batch_until: 这位助理正在攒的那一批什么时候到点（没有在攒的给 None）
    """
    now = aware_utc(context.now)
    if level is Level.HOLD and context.owner_spoke_at is not None:
        return aware_utc(context.owner_spoke_at) + HOLD_WINDOW
    if level is Level.BATCH:
        if batch_until is not None and aware_utc(batch_until) > now:
            return aware_utc(batch_until)
        return now + BATCH_WINDOW
    return None
