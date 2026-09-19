"""消息与群：搜索、读取、会话历史、以本人身份发送与回复，以及群的查询和管理。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    OpenIds,
    Page,
    Search,
    Short,
    Text,
    Uuid,
    compact,
    page,
    tool,
)


class TimedPage(Page):
    start_time: int | None = Field(
        default=None, ge=0, le=9_999_999_999, description="Unix timestamp in seconds"
    )
    end_time: int | None = Field(
        default=None, ge=0, le=9_999_999_999, description="Unix timestamp in seconds"
    )

    @model_validator(mode="after")
    def ordered_times(self) -> TimedPage:
        if self.start_time is not None and self.end_time is not None:
            if self.start_time >= self.end_time:
                raise ValueError("start_time must precede end_time")
        return self


class SearchMessages(Search):
    chat_type: Literal["p2p", "group"] | None = None
    chat_id: Identifier | None = None


class ReadMessages(Arguments):
    message_ids: list[Identifier] = Field(min_length=1, max_length=20)


class ChatHistory(TimedPage):
    chat_id: Identifier


Format = Annotated[
    Literal["text", "markdown"],
    Field(description="markdown for headings, lists, bold and links"),
]


class SendMessage(Arguments):
    receive_id: Identifier
    receive_id_type: Literal["open_id", "user_id", "chat_id"]
    text: Text
    format: Format = "text"
    uuid: Uuid


class ReplyMessage(Arguments):
    message_id: Identifier
    text: Text
    format: Format = "text"
    reply_in_thread: bool = False
    uuid: Uuid


class ForwardMessage(Arguments):
    message_id: Identifier
    receive_id: Identifier
    receive_id_type: Literal["open_id", "user_id", "chat_id"]
    uuid: Uuid


class MessageRef(Arguments):
    message_id: Identifier


class Reaction(MessageRef):
    emoji_type: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]{1,64}$")] = Field(
        description="Feishu emoji key such as THUMBSUP, OK, DONE, SMILE"
    )


class ListChats(Page):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""


class ChatMembers(Page):
    chat_id: Identifier


class CreateChat(Arguments):
    name: Short
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None
    member_open_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    uuid: Uuid


class AddChatMembers(Arguments):
    chat_id: Identifier
    member_open_ids: OpenIds


def _message(text: str, kind: str) -> dict[str, str]:
    if kind == "markdown":
        post = {"zh_cn": {"content": [[{"tag": "md", "text": text}]]}}
        return {"msg_type": "post", "content": json.dumps(post, ensure_ascii=False)}
    return {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}


@tool(
    "feishu_search_messages",
    SearchMessages,
    "im.search",
    "Search own accessible messages and retrieve content.",
)
async def search_messages(args: SearchMessages, call: Call) -> dict[str, Any]:
    body: dict[str, Any] = {"query": args.query}
    filters: dict[str, Any] = {}
    if args.time_range():
        filters["time_range"] = args.time_range()
    if args.chat_type is not None:
        filters["chat_type"] = args.chat_type
    if args.chat_id is not None:
        filters["chat_ids"] = [args.chat_id]
    if filters:
        body["filter"] = filters
    found = await call("im.search", params=page(args), json=body)
    ids: list[str] = []
    for item in found.get("items", [])[: args.limit]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("meta_data") or {}
        candidate = metadata.get("message_id") if isinstance(metadata, dict) else None
        if candidate is None:
            candidate = item.get("message_id")
        if not isinstance(candidate, str):
            continue
        try:
            ids.extend(ReadMessages(message_ids=[candidate]).message_ids)
        except ValidationError:
            continue
    messages = await call("im.mget", params={"message_ids": ids}) if ids else {}
    return {
        "messages": messages.get("items", [])[: args.limit],
        "has_more": bool(found.get("has_more")),
        "page_token": found.get("page_token"),
    }


@tool("feishu_read_messages", ReadMessages, "im.mget", "Read up to 20 accessible messages by ID.")
async def read_messages(args: ReadMessages, call: Call) -> dict[str, Any]:
    data = await call("im.mget", params={"message_ids": args.message_ids})
    if isinstance(data.get("items"), list):
        data = {**data, "items": data["items"][: len(args.message_ids)]}
    return data


@tool(
    "feishu_chat_history",
    ChatHistory,
    "im.history",
    "Read one page of an accessible chat's history.",
)
async def chat_history(args: ChatHistory, call: Call) -> dict[str, Any]:
    times = {"start_time": args.start_time, "end_time": args.end_time}
    return await call(
        "im.history",
        params={
            **page(args),
            **{key: str(value) for key, value in times.items() if value is not None},
            "container_id_type": "chat",
            "container_id": args.chat_id,
        },
    )


@tool(
    "feishu_send_message",
    SendMessage,
    "im.send",
    "Send a text or Markdown message as this user. Reuse uuid for retries.",
)
async def send_message(args: SendMessage, call: Call) -> dict[str, Any]:
    return await call(
        "im.send",
        params={"receive_id_type": args.receive_id_type},
        json={"receive_id": args.receive_id, **_message(args.text, args.format), "uuid": args.uuid},
    )


@tool(
    "feishu_reply_message",
    ReplyMessage,
    "im.reply",
    "Reply to a message as this user, optionally in its thread. Reuse uuid for retries.",
)
async def reply_message(args: ReplyMessage, call: Call) -> dict[str, Any]:
    return await call(
        "im.reply",
        path={"message_id": args.message_id},
        json={
            **_message(args.text, args.format),
            "reply_in_thread": args.reply_in_thread,
            "uuid": args.uuid,
        },
    )


@tool(
    "feishu_forward_message",
    ForwardMessage,
    "im.forward",
    "Forward a message as this user to a person or chat. Reuse uuid for retries.",
)
async def forward_message(args: ForwardMessage, call: Call) -> dict[str, Any]:
    return await call(
        "im.forward",
        path={"message_id": args.message_id},
        params={"receive_id_type": args.receive_id_type, "uuid": args.uuid},
        json={"receive_id": args.receive_id},
    )


@tool(
    "feishu_recall_message",
    MessageRef,
    "im.recall",
    "Recall (withdraw) a message this user sent.",
)
async def recall_message(args: MessageRef, call: Call) -> dict[str, Any]:
    return await call("im.recall", path={"message_id": args.message_id})


@tool("feishu_add_reaction", Reaction, "im.reaction", "Add an emoji reaction as this user.")
async def add_reaction(args: Reaction, call: Call) -> dict[str, Any]:
    return await call(
        "im.reaction",
        path={"message_id": args.message_id},
        json={"reaction_type": {"emoji_type": args.emoji_type}},
    )


@tool("feishu_pin_message", MessageRef, "im.pin", "Pin a message in its chat.")
async def pin_message(args: MessageRef, call: Call) -> dict[str, Any]:
    return await call("im.pin", json={"message_id": args.message_id})


@tool(
    "feishu_list_chats",
    ListChats,
    "im.chats",
    "List chats this user is in, or search them by name or member when query is set.",
)
async def list_chats(args: ListChats, call: Call) -> dict[str, Any]:
    if args.query:
        return await call("im.chat_search", params={**page(args), "query": args.query})
    return await call("im.chats", params=page(args))


@tool("feishu_chat_members", ChatMembers, "im.chat_members", "List a chat's members.")
async def chat_members(args: ChatMembers, call: Call) -> dict[str, Any]:
    return await call(
        "im.chat_members",
        path={"chat_id": args.chat_id},
        params={**page(args), "member_id_type": "open_id"},
    )


@tool(
    "feishu_create_chat",
    CreateChat,
    "im.chat_create",
    "Create a group chat owned by this user with the given members (open_id).",
)
async def create_chat(args: CreateChat, call: Call) -> dict[str, Any]:
    return await call(
        "im.chat_create",
        params={"user_id_type": "open_id", "uuid": args.uuid},
        json=compact(
            {
                "name": args.name,
                "description": args.description,
                "user_id_list": args.member_open_ids or None,
            }
        ),
    )


@tool(
    "feishu_add_chat_members",
    AddChatMembers,
    "im.chat_add_members",
    "Add people (open_id) to a chat.",
)
async def add_chat_members(args: AddChatMembers, call: Call) -> dict[str, Any]:
    return await call(
        "im.chat_add_members",
        path={"chat_id": args.chat_id},
        params={"member_id_type": "open_id"},
        json={"id_list": args.member_open_ids},
    )


TOOLS = [
    search_messages,
    read_messages,
    chat_history,
    send_message,
    reply_message,
    forward_message,
    recall_message,
    add_reaction,
    pin_message,
    list_chats,
    chat_members,
    create_chat,
    add_chat_members,
]
