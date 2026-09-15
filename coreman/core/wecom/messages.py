"""平台无关的入站消息模型。

网关把各平台的回调归一化成 `InboundMessage` 落进 `inbound_events.payload`，worker 只读这一
份模型，不认识任何平台字段。模型故意保持「贫血」：网关不解析身份（`sender` 只有平台 id）、
不下载媒体（`ref` 是平台资源引用，由 worker 的 MediaFetcher 处理）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Sender(BaseModel):
    """发言者的平台标识；映射到 CoreMan 用户是 worker 的事（`resolve_speaker`）。"""

    platform_user_id: str
    open_id: str | None = None
    display_name: str | None = None


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    ref: dict[str, Any] = Field(default_factory=dict)


class FilePart(BaseModel):
    type: Literal["file"] = "file"
    ref: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None


class AudioPart(BaseModel):
    """语音：企微已经转好文字，`transcript` 非空时等同文本（见 worker 的 `part_kind`）。"""

    type: Literal["audio"] = "audio"
    ref: dict[str, Any] = Field(default_factory=dict)
    transcript: str | None = None


class VideoPart(BaseModel):
    """视频：本期不支持，只为把类型记进 chat_logs.message_type 与给用户一个准确的提示。"""

    type: Literal["video"] = "video"
    ref: dict[str, Any] = Field(default_factory=dict)


class QuotePart(BaseModel):
    """被引用的那条消息；`kind` 是它自己的类型（text/image/file/…）。"""

    type: Literal["quote"] = "quote"
    kind: str = "text"
    text: str | None = None
    refs: list[dict[str, Any]] = Field(default_factory=list)


Part = Annotated[
    TextPart | ImagePart | FilePart | AudioPart | VideoPart | QuotePart,
    Field(discriminator="type"),
]


class InboundMessage(BaseModel):
    """一条归一化的入站事件（消息、进入会话、卡片回调、点赞反馈）。"""

    model_config = ConfigDict(extra="forbid")

    platform: Literal["wecom", "feishu"]
    bot_id: uuid.UUID
    kind: Literal["message", "card_action", "enter_chat", "feedback", "bot_added"]
    chat_type: Literal["single", "group"]
    chat_id: str
    sender: Sender
    message_id: str
    mentions_bot: bool = False
    parts: list[Part] = Field(default_factory=list)
    card_action: dict[str, Any] | None = None
    reply_context: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


def text_of(message: InboundMessage) -> str:
    """文本类内容拼成一段：文本 part 原文 + 语音 part 的转写。"""
    pieces: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart) and part.text:
            pieces.append(part.text)
        elif isinstance(part, AudioPart) and part.transcript:
            pieces.append(part.transcript)
    return "\n".join(pieces)


def _kind_of(part: Part) -> str:
    # 语音对外一律叫 voice（企微的 msgtype、chat_logs.message_type 都是这个词）。
    return "voice" if isinstance(part, AudioPart) else part.type


def message_type_of(message: InboundMessage) -> str:
    """`chat_logs.message_type`：引用优先 `quote_*`，单一类型取该类型，多类型混排 mixed。"""
    for part in message.parts:
        if isinstance(part, QuotePart):
            return f"quote_{part.kind or 'text'}"
    kinds = {_kind_of(p) for p in message.parts}
    if not kinds:
        return "text"
    return kinds.pop() if len(kinds) == 1 else "mixed"


class CardAction(BaseModel):
    """模板卡片事件的归一化：`selected` 是 `question_key → 选项 id 列表`。"""

    task_id: str
    card_type: str
    event_key: str
    selected: dict[str, list[str]] = Field(default_factory=dict)


def parse_card_event(event: dict[str, Any]) -> CardAction | None:
    """企微 `body.event`（eventtype=template_card_event）→ CardAction；没有 task_id 视为无效。"""
    if str(event.get("eventtype") or "") != "template_card_event":
        return None
    raw = event.get("template_card_event")
    if not isinstance(raw, dict) or not raw.get("task_id"):
        return None
    selected: dict[str, list[str]] = {}
    items = (raw.get("selected_items") or {}).get("selected_item") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("question_key") or "")
        ids = (item.get("option_ids") or {}).get("option_id") or []
        selected[key] = [str(i) for i in ids if isinstance(i, str | int)]
    return CardAction(
        task_id=str(raw["task_id"]),
        card_type=str(raw.get("card_type") or ""),
        event_key=str(raw.get("event_key") or ""),
        selected=selected,
    )
