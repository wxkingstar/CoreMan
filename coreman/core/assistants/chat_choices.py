"""给所有者挑「不托管 / 只托管」会话用的会话清单：编号、名字与类型，不读任何消息。

不托管的会话在个人令牌的入口被滤掉（`chat_guard`），这里在 `chat_guard.bypass()` 里列：所有者
要能看到并勾回已经划出去的会话。清单只交给所有者本人（「我的助理」页、他私聊里的卡片），
不落库、不进任何一轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coreman.core.assistants import chat_guard
from coreman.core.assistants import watch_feed as feed
from coreman.core.feishu_personal.errors import PersonalError

LIMIT = 100
# 单聊在会话列表里多半没有名字：用成员接口补对方的名字，一次最多补这么多个。
P2P_NAMES = 30
MAX_NAME = 60


@dataclass(frozen=True)
class Choice:
    chat_id: str
    name: str
    kind: str

    def payload(self) -> dict[str, str]:
        return {"chat_id": self.chat_id, "name": self.name, "kind": self.kind}


def _rows(data: Any) -> list[dict[str, Any]]:
    found = data.get("items") if isinstance(data, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


async def _peer(call: feed.Reader, chat_id: str, owner_open_id: str) -> str:
    try:
        data = await call(
            "im.chat_members",
            path={"chat_id": chat_id},
            params={"member_id_type": "open_id", "page_size": 5},
        )
    except PersonalError:
        return ""
    for member in _rows(data):
        if member.get("member_id") != owner_open_id and member.get("name"):
            return str(member["name"])
    return ""


async def listing(
    call: feed.Reader, *, own_chat: str, owner_open_id: str, query: str = ""
) -> list[Choice]:
    """最近活跃的会话（给了 `query` 时按名字或成员搜），不含所有者与助理的私聊。"""
    with chat_guard.bypass():
        if query:
            data = await call(
                "im.chat_search",
                params={"query": query[:64], "page_size": LIMIT, "user_id_type": "open_id"},
            )
        else:
            data = await call("im.chats", params={**feed.LIST_PARAMS, "page_size": LIMIT})
        found: list[Choice] = []
        seen: set[str] = set()
        named = 0
        for item in _rows(data):
            chat_id = item.get("chat_id")
            if not isinstance(chat_id, str) or not chat_id or chat_id == own_chat:
                continue
            if chat_id in seen:
                continue
            seen.add(chat_id)
            kind = "p2p" if item.get("chat_mode") == "p2p" else "group"
            name = str(item.get("name") or "")
            if not name and kind == "p2p" and named < P2P_NAMES:
                named += 1
                name = await _peer(call, chat_id, owner_open_id)
            found.append(Choice(chat_id, name[:MAX_NAME], kind))
    return found[:LIMIT]


def label(choice: Choice) -> str:
    """给所有者看的一行：类型加名字，没名字时给编号。"""
    head = "单聊" if choice.kind == "p2p" else "群"
    return f"{head}：{choice.name or choice.chat_id}"
