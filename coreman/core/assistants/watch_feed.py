"""盯消息的读取：列会话、对比顺序找出有新动静的会话、读历史。

只调读接口（列会话、会话信息、会话历史、按编号取消息），都经 `toolbase.Call`，以所有者的身份。
拉到的消息只在调用方的内存里；这里不碰库。

飞书返回的字段有几处没在真实租户里核对过（见实现计划「需真机确认」），所以每个字段都按
「可能没有」来读：缺了就退到保守的做法，不抛错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coreman.core.assistants.approvals import checks
from coreman.core.assistants.watch_grade import Item
from coreman.core.feishu_personal.service import PersonalError

Reader = checks.Reader
# 一次列出单聊与群。spec「前置验证」只实测过 `types=p2p`，这个取值待真机确认。
CHAT_TYPES = "p2p,group"
TOP = 50
LIST_PARAMS: dict[str, Any] = {
    "user_id_type": "open_id",
    "sort_type": "ByActiveTimeDesc",
    "page_size": TOP,
    "types": CHAT_TYPES,
}
PAGE = 50
# 一个会话一轮最多读这么多页；读不完的下一轮从游标接着读。
MAX_PAGES = 2
THREAD_PAGE = 20
# 每个会话跟着的话题数：读这个会话时顺带读它们。
THREADS_TRACKED = 3
CURSOR_CHATS = 200
RECALL_BATCH = 20


@dataclass(frozen=True)
class Listed:
    """列会话的结果：按最近活跃倒序的会话 id，以及顺带拿到的名字与类型。"""

    order: list[str]
    names: dict[str, str]
    kinds: dict[str, str]


def _rows(data: Any, key: str = "items") -> list[dict[str, Any]]:
    """应答里的对象列表：字段缺了、不是列表、里面混着别的东西，都只留下是对象的那些。"""
    found = data.get(key) if isinstance(data, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def _kind(mode: Any) -> str | None:
    if mode == "p2p":
        return "p2p"
    return "group" if isinstance(mode, str) and mode else None


async def list_chats(call: Reader) -> Listed:
    data = await call("im.chats", params=dict(LIST_PARAMS))
    order: list[str] = []
    names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for item in _rows(data):
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id or chat_id in names:
            continue
        order.append(chat_id)
        names[chat_id] = str(item.get("name") or "")
        kind = _kind(item.get("chat_mode"))
        if kind is not None:
            kinds[chat_id] = kind
    return Listed(order[:TOP], names, kinds)


def split(order: list[str], prev: list[str]) -> tuple[list[str], list[str]]:
    """把这一轮的顺序切成（挪动过的前缀, 相对顺序没变的后缀）。

    上一轮之后有新消息的会话都会排到没有新消息的前面，所以没有新消息的会话是一段后缀，彼此的
    先后与上一轮相同。从末尾往前找这段最长的后缀；没见过的会话夹在其中不打断它（上面有会话
    退出时末尾会补进旧会话），但排在最前面的没见过的会话算新动静。

    后缀开头的几个仍可能藏着新消息（它们来了消息但名次没变），由调用方从头逐个探。
    """
    rank = {chat: index for index, chat in enumerate(prev)}
    cut, floor = len(order), len(prev)
    for index in range(len(order) - 1, -1, -1):
        position = rank.get(order[index])
        if position is not None:
            if position >= floor:
                break
            floor = position
        cut = index
    while cut < len(order) and order[cut] not in rank:
        cut += 1
    return order[:cut], order[cut:]


def to_item(
    raw: dict[str, Any],
    *,
    chat_id: str,
    chat_kind: str,
    chat_name: str,
    thread_id: str | None = None,
) -> Item | None:
    """飞书的一条消息；没有编号或读不出时间的返回 None。"""
    parsed = checks.parse(raw)
    try:
        at_ms = int(raw.get("create_time") or 0)
    except (TypeError, ValueError):
        return None
    if not parsed.message_id or at_ms <= 0:
        return None
    own_thread = raw.get("thread_id")
    return Item(
        chat_id=chat_id,
        chat_kind=chat_kind,
        message_id=parsed.message_id,
        sender_id=parsed.sender_id,
        sender_type=parsed.sender_type,
        at_ms=at_ms,
        thread_id=thread_id or (own_thread if isinstance(own_thread, str) and own_thread else None),
        msg_type=str(raw.get("msg_type") or ""),
        text=parsed.text,
        mentions=parsed.mentions,
        deleted=parsed.deleted,
        chat_name=chat_name,
    )


def fresh(last: int, same: list[str], item: Item) -> bool:
    """这条消息在游标之后：晚于 `last`，或与它同一毫秒但不在已读过的 `same` 里。"""
    return item.at_ms > last or (item.at_ms == last and item.message_id not in same)


async def read_main(call: Reader, chat_id: str, since_ms: int) -> tuple[list[dict[str, Any]], int]:
    """会话主线里 `since_ms` 所在那一秒起的消息，从旧到新；另返回调了几次接口。

    `start_time` 以秒计，所以会把游标那一秒的消息再读回来，由调用方用 `fresh` 滤掉。
    """
    params: dict[str, Any] = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": str(since_ms // 1000),
        "sort_type": "ByCreateTimeAsc",
        "page_size": PAGE,
    }
    items: list[dict[str, Any]] = []
    calls = 0
    for _ in range(MAX_PAGES):
        data = await call("im.history", params=dict(params))
        calls += 1
        items.extend(_rows(data))
        token = data.get("page_token") if data.get("has_more") else None
        if not isinstance(token, str) or not token:
            break
        params["page_token"] = token
    return items, calls


async def read_thread(call: Reader, thread_id: str) -> list[dict[str, Any]]:
    """话题里最近的一页，从旧到新。话题容器不带时间范围，由调用方按时间过滤。"""
    data = await call(
        "im.history",
        params={
            "container_id_type": "thread",
            "container_id": thread_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": THREAD_PAGE,
        },
    )
    items = _rows(data)
    items.reverse()
    return items


async def chat_kind(call: Reader, chat_id: str) -> tuple[str, str]:
    """列会话没给类型时问会话本身：（`p2p` 或 `group`，会话名）。"""
    info = await call("im.chat", path={"chat_id": chat_id}, params={"user_id_type": "open_id"})
    return ("p2p" if info.get("chat_mode") == "p2p" else "group"), str(info.get("name") or "")


async def recalled(call: Reader, message_ids: list[str]) -> list[str]:
    """这些消息里已被对方撤回的。一条都读不到（不可见、已不存在）不算撤回。"""
    if not message_ids:
        return []
    try:
        data = await call("im.mget", params={"message_ids": message_ids[:RECALL_BATCH]})
    except PersonalError as exc:
        if exc.code != "feishu_read_failed":
            raise
        return []
    return [
        str(item["message_id"])
        for item in _rows(data)
        if item.get("deleted") and item.get("message_id")
    ]


def load(raw: dict[str, Any]) -> dict[str, Any]:
    """库里的游标规整成一份可以随便改的副本。"""
    chats = raw.get("chats")
    todo = raw.get("todo")
    since = raw.get("since")
    probe = raw.get("p")
    oldest = raw.get("a")
    return {
        "since": since if isinstance(since, int) else 0,
        # 没探完的那一段会话与那一轮的年龄截止点（毫秒）：见 `watch._round`。
        "p": [chat for chat in (probe if isinstance(probe, list) else []) if isinstance(chat, str)],
        "a": oldest if isinstance(oldest, int) else 0,
        "chats": {
            str(chat): dict(entry)
            for chat, entry in (chats if isinstance(chats, dict) else {}).items()
            if isinstance(entry, dict)
        },
        "todo": [
            chat for chat in (todo if isinstance(todo, list) else []) if isinstance(chat, str)
        ],
    }


def dump(cursor: dict[str, Any], todo: list[str]) -> dict[str, Any]:
    """写回库的游标：只留最近读过的 `CURSOR_CHATS` 个会话；还在排队的、还没探到的会话先留
    （它们记着起点）。"""
    chats: dict[str, dict[str, Any]] = cursor["chats"]
    waiting = {*todo[:TOP], *(cursor.get("p") or [])[:TOP]}
    kept = sorted(
        chats, key=lambda chat: (chat in waiting, int(chats[chat].get("t") or 0)), reverse=True
    )
    stored = {
        "since": cursor["since"],
        "chats": {chat: chats[chat] for chat in kept[:CURSOR_CHATS]},
        "todo": todo[:TOP],
    }
    if cursor.get("p"):
        # 还有一段会话没探到：连同那一轮的年龄截止点一起留着，探完才去掉。
        stored["p"], stored["a"] = cursor["p"][:TOP], int(cursor.get("a") or 0)
    return stored
