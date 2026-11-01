"""内置命令关键字。"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

RESET_WORDS = ("reset", "new", "clear", "重置", "清空")
STOP_WORDS = ("stop", "停止", "暂停", "停")
HELP_WORDS = ("help", "帮助", "?", "？")
# 群聊旁听：群里 @ 机器人说这些，它在这个群暂时只在被 @ 时说话 / 恢复主动发言。
QUIET_WORDS = ("安静", "别说话", "闭嘴", "quiet", "mute")
UNQUIET_WORDS = ("可以说话了", "unmute")
GATEWAY_COMMANDS = ("reset", "stop")
CANCEL_WORDS = ("取消", "cancel")
Command = Literal["reset", "stop", "help", "quiet", "unquiet"]
_KEEP = {"\n", "\r", "\t", " "}


def normalize(text: str) -> str:
    kept = "".join(ch for ch in text if ch in _KEEP or not unicodedata.category(ch).startswith("C"))
    return kept.strip().lower()


def command_text(text: str) -> str:
    """命令匹配用的文本。飞书斜杠指令把所选指令作为普通文本「/new」发来：去掉一个前导斜杠。"""
    s = normalize(text)
    return s[1:].strip() if s.startswith("/") else s


def classify_command(text: str) -> Command | None:
    s = command_text(text)
    if not s:
        return None
    if s in RESET_WORDS:
        return "reset"
    if s in HELP_WORDS:
        return "help"
    if s in QUIET_WORDS:
        return "quiet"
    if s in UNQUIET_WORDS:
        return "unquiet"
    if re.sub(r"[^\w一-鿿]", "", s) in STOP_WORDS:
        return "stop"
    return None


def is_cancel_word(text: str) -> bool:
    """待答交互的取消词：精确匹配（去首尾空白与不可见字符、lower）。

    只认整句「取消 / cancel」：「取消订单」是要 AI 干活，不是要退出这一轮提问。
    """
    return normalize(text).lower() in CANCEL_WORDS
