"""入站消息 parts 的归一化小工具。"""

from __future__ import annotations

import re
from typing import Any


def part_kind(part: dict[str, Any]) -> str:
    """归一化 part 类别：带转写的语音等同文本，其余原样。

    企微网关发 `AudioPart(type="audio")`，飞书侧历史上叫 `voice`：两个名字都认，
    否则「企微已经转好文字的语音」会被当成不支持的消息类型挡在门外。
    """
    kind = str(part.get("type") or "")
    if kind in ("voice", "audio") and part.get("transcript"):
        return "text"
    return kind


def message_type_of(parts: list[dict[str, Any]]) -> str:
    """chat_logs.message_type：全文本 text，单一非文本类型取该类型，混排 mixed。"""
    for part in parts:
        if part.get("type") == "quote":
            return f"quote_{part.get('kind') or 'text'}"
    others = sorted({part_kind(p) for p in parts} - {"text"})
    if not others:
        return "text"
    return others[0] if len(others) == 1 else "mixed"


def joined_text(parts: list[dict[str, Any]]) -> str:
    """把文本类 part 拼成一段；语音取转写。"""
    pieces = [
        str(p.get("text") or p.get("transcript") or "") for p in parts if part_kind(p) == "text"
    ]
    return "\n".join(p for p in pieces if p)


def strip_mention(text: str, bot_name: str) -> str:
    """去掉群里 @机器人 留下的名字，后面的命令匹配才认得出 `@机器人 stop`。"""
    if not bot_name:
        return text
    return re.sub(rf"@{re.escape(bot_name)}\s?", "", text)
