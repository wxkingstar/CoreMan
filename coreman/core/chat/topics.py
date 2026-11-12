"""飞书群里的话题：每个话题一条自己的会话，第一次在话题里找机器人时问一句要不要接着群里聊的。

会话键是 `<chat_id>#topic:<thread_id>`，主聊天区仍是 `chat_id`。话题里第一条交给机器人的
消息先挂起，引用它发一张卡片让本人选：

- 独立开启新会话：话题从空会话开始。
- 继承群主会话：新会话从群主会话分叉出来（运行时原生 fork；旧节点退回附上群主会话最近的对话），
  之后话题和主聊天区各聊各的。

群主会话不存在（没聊过、已过期）时不问，直接独立。没人点也不卡住：话题里又来了消息，或者挂起
到期，都按独立处理。卡片的 task_id 是 `topic@<挂起的任务 id>`。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.bus.notify import notify
from coreman.core.db.models import ChatSession, RuntimeNode, Task
from coreman.core.i18n.messages import msg

SEPARATOR = "#topic:"
CARD_PREFIX = "topic@"
NEW = "topic_new"
FORK = "topic_fork"
# 运行时原生分叉会话的能力位（驱动在 /health 上报）。
RUNTIME_CAPABILITY = "session_fork_v1"
# 卡片最多等这么久，没人选就按独立处理。
HOLD_SECONDS = 5 * 60
_TASK_ID = re.compile(r"topic@([1-9][0-9]{0,18})")
FORK_HEADER = (
    "- 上下文续接：这个话题是从群里的主会话分出来的，提问人选择接着群里的上下文聊。下面是群主会话"
    "最近的对话（按时间顺序，可能有截断），请据此理解话题里的问题，不要向用户复述或提及这段记录。"
)


def session_key(chat_id: str, thread_id: str) -> str:
    return f"{chat_id}{SEPARATOR}{thread_id}"


def key_for(platform: str, chat_type: str, chat_id: str, reply_context: dict[str, Any]) -> str:
    """一条消息归哪个会话：飞书群话题里的归话题，其余归会话本身。"""
    thread = reply_context.get("thread_id")
    if platform == "feishu" and chat_type == "group" and isinstance(thread, str) and thread:
        return session_key(chat_id, thread)
    return chat_id


def chat_of(key: str) -> str:
    """会话键所在的群（或单聊）：话题键去掉话题部分。"""
    return key.split(SEPARATOR, 1)[0]


def is_topic(key: str | None) -> bool:
    return bool(key) and SEPARATOR in str(key)


def same_conversation(column: Any, key: str) -> Any:
    """SQL 条件：记录的会话键和 `key` 属于同一段对话。

    话题只认自己；主聊天区认所有不在话题里的（调度员派的任务会话等也算群里的）。
    """
    return column == key if is_topic(key) else ~column.contains(SEPARATOR)


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


async def should_offer(session: AsyncSession, bot_id: uuid.UUID, key: str) -> bool:
    """话题还没有会话、群主会话在，且话题里没有别的待处理消息：要问。"""
    if not is_topic(key):
        return False
    if await session.get(ChatSession, (bot_id, key)) is not None:
        return False
    if await session.get(ChatSession, (bot_id, chat_of(key))) is None:
        return False
    pending = await session.scalar(
        select(Task.id)
        .where(
            Task.bot_id == bot_id,
            Task.session_key == key,
            Task.kind == "chat",
            Task.status.in_(tasks.OPEN),
        )
        .limit(1)
    )
    return pending is None


def hold_until() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=HOLD_SECONDS)


async def choose(session: AsyncSession, bot_id: uuid.UUID, key: str, *, fork: bool) -> bool:
    """记下这个话题的会话；返回是否真的从群主会话分叉。

    先建好话题的会话行：之后话题里再来消息就不会再问。群主会话已经没了，就只能独立。
    """
    main = await session.get(ChatSession, (bot_id, chat_of(key)), populate_existing=True)
    if main is None:
        return False
    forked = main.relay_session_id if fork else None
    await session.execute(
        insert(ChatSession)
        .values(
            bot_id=bot_id,
            session_key=key,
            relay_session_id=uuid.uuid4(),
            backend=main.backend,
            last_active_at=func.now(),
            forked_from=forked,
        )
        .on_conflict_do_nothing(index_elements=[ChatSession.bot_id, ChatSession.session_key])
    )
    return forked is not None


async def release(session: AsyncSession, task: Task) -> None:
    """挂起的消息放行：立刻可认领。调用方持有行锁。"""
    task.payload = {k: v for k, v in task.payload.items() if k != "topic_hold"}
    task.run_after = func.now()
    await session.flush()
    await notify(session, "tasks_queued", {"lane": task.lane, "bot_id": str(task.bot_id)})


async def release_pending(session: AsyncSession, bot_id: uuid.UUID, key: str) -> bool:
    """卡片还没人选，话题里又来了消息：按独立处理，放行挂起的那条；返回有没有挂起的。"""
    held = await session.scalar(
        select(Task)
        .where(
            Task.bot_id == bot_id,
            Task.session_key == key,
            Task.kind == "chat",
            Task.status == "queued",
            Task.cancel_requested_at.is_(None),
            Task.payload["topic_hold"].astext == "true",
        )
        .order_by(Task.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if held is None:
        return False
    await choose(session, bot_id, key, fork=False)
    await release(session, held)
    return True


async def fork_supported(session: AsyncSession, node_id: uuid.UUID | None, backend: str) -> bool:
    """运行时能不能原生分叉会话；旧节点退回附上群主会话最近的对话。"""
    if node_id is None:
        return False
    node = await session.get(RuntimeNode, node_id, populate_existing=True)
    if node is None or not node.is_active:
        return False
    capability = (node.capabilities or {}).get(backend) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True


def offer_card(task_id: int, *, locale: str = "zh") -> dict[str, Any]:
    tid = card_task_id(task_id)

    def button(key: str, label: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": msg(label, locale)},
            "type": kind,
            "width": "fill",
            "behaviors": [{"type": "callback", "value": {"task_id": tid, "event_key": key}}],
        }

    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": msg("topic_title", locale)},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": msg("topic_ask", locale)
                    + "\n<font color='grey'>"
                    + msg("topic_default", locale, m=HOLD_SECONDS // 60)
                    + "</font>",
                },
                button(NEW, "topic_opt_new", "primary"),
                button(FORK, "topic_opt_fork", "default"),
            ]
        },
        "task_id": tid,
    }
