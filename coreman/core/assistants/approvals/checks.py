"""提交前与执行前的检查：平台用所有者的令牌重新读会话，不采信助理对会话状态的说法。

这里只调读接口（会话信息、会话历史、按编号取消息、通讯录），不受写操作保险丝影响。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

from coreman.core.assistants.approvals import catalog
from coreman.core.assistants.approvals.content import Facts
from coreman.core.feishu_personal.service import PersonalError

# 重新读会话时取最近多少条；锚点比这更早就算过期。
RECENT = 50
HINTS = {
    "chat_id_required": "消息类操作要在 approval 里带上 chat_id 与 anchor_message_id。",
    "receive_id_must_be_chat": "receive_id_type 用 chat_id，receive_id 与 approval.chat_id 相同。",
    "anchor_required": "这个会话里已有消息：approval.anchor_message_id 填你读到的最后一条。",
    "anchor_not_found": "锚点不在会话最近的消息里（太早或已撤回）：重新读会话后再起草。",
    "owner_replied_after_anchor": "锚点之后本人已经在这个会话里发过言：读完最新消息后重写或放弃。",
    "newer_messages_after_anchor": (
        "锚点之后对方又发了消息（群里指 @ 本人、回复锚点那条、或锚点发送人自己又发的）："
        "读完最新消息后重写。"
    ),
    "message_gone": "目标消息不在这个会话里，或已被撤回。",
    "not_own_message": "只能编辑或撤回本人发出的消息。",
}
# 会话读取：`toolbase.Call`，或测试里签名相同的替身。
Reader = Callable[..., Awaitable[dict[str, Any]]]
# 给一批消息编号，返回其中由平台经审批替所有者发出的（`sent.among`）。
Ours = Callable[[list[str]], Awaitable[Collection[str]]]


class Rejected(Exception):
    """这次调用不能提交（执行前复查时：不能执行）。`code` 会返回给助理。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.hint = HINTS.get(code, "")


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    sender_id: str
    sender_type: str
    text: str
    deleted: bool
    # 被 @ 的人、它回复的那条消息、所在话题的根消息：判断群里的新消息与锚点有没有关系时用。
    mentions: frozenset[str] = frozenset()
    parent_id: str = ""
    root_id: str = ""


@dataclass(frozen=True)
class ChatView:
    """一个会话（或话题）此刻的样子；`messages` 从新到旧。"""

    kind: str
    title: str
    messages: list[Message]


def _text(item: dict[str, Any]) -> str:
    kind = str(item.get("msg_type") or "")
    body = item.get("body")
    raw = body.get("content") if isinstance(body, dict) else None
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    if kind == "text" and isinstance(data, dict):
        return str(data.get("text") or "")
    if kind == "post" and isinstance(data, dict):
        runs = [
            str(run.get("text") or "")
            for line in data.get("content") or []
            if isinstance(line, list)
            for run in line
            if isinstance(run, dict)
        ]
        return " ".join(part for part in [str(data.get("title") or ""), *runs] if part)
    return f"[{kind or '未知类型'}消息]"


def mentioned(item: dict[str, Any]) -> frozenset[str]:
    """消息里被 @ 的人。`id` 按字符串读；万一是按类型分开的对象，取其中的 open_id。

    @所有人不产生指向某个人的条目，不算 @ 了谁。
    """
    found: set[str] = set()
    raw = item.get("mentions")
    for one in raw if isinstance(raw, list) else []:
        who = one.get("id") if isinstance(one, dict) else None
        if isinstance(who, dict):
            who = who.get("open_id")
        if isinstance(who, str) and who:
            found.add(who)
    return frozenset(found)


def parse(item: dict[str, Any]) -> Message:
    sender = item.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    return Message(
        message_id=str(item.get("message_id") or ""),
        chat_id=str(item.get("chat_id") or ""),
        sender_id=str(sender.get("id") or ""),
        sender_type=str(sender.get("sender_type") or ""),
        text=_text(item),
        deleted=bool(item.get("deleted")),
        mentions=mentioned(item),
        parent_id=str(item.get("parent_id") or ""),
        root_id=str(item.get("root_id") or ""),
    )


async def chat_view(call: Reader, chat_id: str, thread_id: str | None = None) -> ChatView:
    info = await call("im.chat", path={"chat_id": chat_id}, params={"user_id_type": "open_id"})
    data = await call(
        "im.history",
        params={
            "container_id_type": "thread" if thread_id else "chat",
            "container_id": thread_id or chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": RECENT,
        },
    )
    items = [parse(item) for item in data.get("items") or [] if isinstance(item, dict)]
    kind = "p2p" if info.get("chat_mode") == "p2p" else "group"
    return ChatView(kind, str(info.get("name") or ""), items)


async def message(call: Reader, message_id: str) -> Message | None:
    """按编号取一条消息；读不到（不存在、不可见）返回 None。"""
    try:
        data = await call("im.mget", params={"message_ids": [message_id]})
    except PersonalError as exc:
        if exc.code != "feishu_read_failed":
            raise
        return None
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    return parse(items[0]) if items else None


async def _name(call: Reader, open_id: str) -> str:
    if not open_id:
        return ""
    try:
        data = await call(
            "contact.user", path={"user_id": open_id}, params={"user_id_type": "open_id"}
        )
    except PersonalError:
        return ""
    user = data.get("user")
    return str(user.get("name") or "") if isinstance(user, dict) else ""


async def _title(call: Reader, view: ChatView, owner_open_id: str) -> str:
    if view.kind == "group":
        return view.title or "未命名群聊"
    peer = next(
        (
            m.sender_id
            for m in view.messages
            if m.sender_type == "user" and m.sender_id != owner_open_id
        ),
        "",
    )
    return await _name(call, peer) or "对方未发言的私聊"


async def _quote(call: Reader, found: Message | None) -> str:
    if found is None:
        return ""
    return f"{await _name(call, found.sender_id) or '（未知发送人）'}：{found.text}"


def anchor_verdict(
    view: ChatView,
    anchor_message_id: str,
    owner_open_id: str,
    ours: Collection[str] = frozenset(),
) -> str | None:
    """锚点之后会话有没有动过；None 表示可以提交（或执行）。

    只数真人发的、没被撤回的消息：机器人的通知不算「对方又发了消息」。`ours` 是平台经审批
    替所有者发出的消息的编号：它们在飞书里发送人是所有者，但不是他亲自回复，也不是对方的新
    消息——同一个会话有两张待批单时，第一张发出去不该让第二张过时。

    所有者亲自发言在哪种会话里都算。别人的新消息，私聊里每一条都算（都是对所有者说的）；
    群里只算与这个话头有关的：@ 了所有者、回复了锚点那条（或在以它为根的话题里）、或锚点的
    发送人自己又发的。群里别人接着聊别的不算——否则活跃的群里，草稿永远赶不上最新一条。
    """
    newer: list[Message] = []
    anchor: Message | None = None
    for item in view.messages:
        if item.message_id == anchor_message_id:
            anchor = item
            break
        newer.append(item)
    if anchor is None:
        return "anchor_not_found"
    people = [
        item
        for item in newer
        if item.sender_type == "user" and not item.deleted and item.message_id not in ours
    ]
    if any(item.sender_id == owner_open_id for item in people):
        return "owner_replied_after_anchor"
    if view.kind == "group":
        people = [item for item in people if _about(item, anchor, owner_open_id)]
    return "newer_messages_after_anchor" if people else None


def _about(item: Message, anchor: Message, owner_open_id: str) -> bool:
    """群里的一条新消息与锚点这个话头有没有关系。"""
    return (
        owner_open_id in item.mentions
        or anchor.message_id in (item.parent_id, item.root_id)
        or (bool(anchor.sender_id) and item.sender_id == anchor.sender_id)
    )


async def _send(
    call: Reader, tool: str, one: dict[str, Any], owner_open_id: str, ours: Ours | None
) -> Facts:
    args, chat_id = one["arguments"], one.get("chat_id")
    if not chat_id:
        raise Rejected("chat_id_required")
    # 发文件时带 reply_to_message_id 就是回复那条消息，规则与 feishu_reply_message 相同。
    reply = tool == "feishu_reply_message" or bool(args.get("reply_to_message_id"))
    source_id = args.get("message_id") or args.get("reply_to_message_id")
    to_chat = args.get("receive_id_type") == "chat_id" and args.get("receive_id") == chat_id
    if not reply and not to_chat:
        raise Rejected("receive_id_must_be_chat")
    view = await chat_view(call, chat_id, one.get("thread_id"))
    by_id = {item.message_id: item for item in view.messages}
    source: Message | None = None
    if source_id:
        # 回复的那条要在这个会话里；转发的来源可以在别的会话，但必须读得到。
        source = by_id.get(source_id) or await message(call, source_id)
        if source is None or source.deleted or (reply and source.chat_id != chat_id):
            raise Rejected("message_gone")
    anchor = one.get("anchor_message_id") or (source_id if reply else None)
    if anchor is None:
        if view.messages:
            raise Rejected("anchor_required")
    else:
        mine = await ours([item.message_id for item in view.messages]) if ours else frozenset()
        verdict = anchor_verdict(view, anchor, owner_open_id, mine)
        if verdict is not None:
            raise Rejected(verdict)
    quoted = source if source is not None else by_id.get(anchor or "")
    return Facts(
        view.kind, await _title(call, view, owner_open_id), await _quote(call, quoted), anchor
    )


async def _on_message(call: Reader, tool: str, one: dict[str, Any], owner_open_id: str) -> Facts:
    args, chat_id = one["arguments"], one.get("chat_id")
    if not chat_id:
        raise Rejected("chat_id_required")
    found = await message(call, args["message_id"])
    if found is None or found.deleted or found.chat_id != chat_id:
        raise Rejected("message_gone")
    if tool in catalog.OWN_MESSAGE and found.sender_id != owner_open_id:
        raise Rejected("not_own_message")
    view = await chat_view(call, chat_id, one.get("thread_id"))
    return Facts(
        view.kind,
        await _title(call, view, owner_open_id),
        await _quote(call, found),
        found.message_id,
    )


async def check_call(
    call: Reader,
    tool: str,
    one: dict[str, Any],
    *,
    owner_open_id: str,
    ours: Ours | None = None,
) -> Facts:
    """检查一次调用并取回审批单要展示的事实；提交前与执行前用的是同一套。

    Args:
        one: `{"arguments": 工具参数, "chat_id": …, "anchor_message_id": …, "thread_id": …}`
        ours: 查哪些消息是平台替所有者发的；给了，这些消息就不算锚点之后的新动静

    Raises:
        Rejected: 不能提交。执行前复查时抛出就记为 `stale`
        PersonalError: 所有者的授权失效等，原样交给调用方
    """
    if tool in catalog.SENDS:
        return await _send(call, tool, one, owner_open_id, ours)
    if tool in catalog.ON_MESSAGE:
        return await _on_message(call, tool, one, owner_open_id)
    channel = catalog.entry(tool).channel
    return Facts("other" if channel == "message" else channel)
