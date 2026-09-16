"""内置命令关键字。"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

RESET_WORDS = ("reset", "new", "clear", "重置", "清空")
STOP_WORDS = ("stop", "停止", "暂停", "停")
HELP_WORDS = ("help", "帮助", "?", "？")
GATEWAY_COMMANDS = ("reset", "stop")
CANCEL_WORDS = ("取消", "cancel")
Command = Literal["reset", "stop", "help"]
_KEEP = {"\n", "\r", "\t", " "}


def normalize(text: str) -> str:
    kept = "".join(ch for ch in text if ch in _KEEP or not unicodedata.category(ch).startswith("C"))
    return kept.strip().lower()


def classify_command(text: str) -> Command | None:
    s = normalize(text)
    if not s:
        return None
    if s in RESET_WORDS:
        return "reset"
    if s in HELP_WORDS:
        return "help"
    if re.sub(r"[^\w一-鿿]", "", s) in STOP_WORDS:
        return "stop"
    return None


def is_cancel_word(text: str) -> bool:
    """待答交互的取消词：精确匹配（去首尾空白与不可见字符、lower）。

    只认整句「取消 / cancel」：「取消订单」是要 AI 干活，不是要退出这一轮提问。
    """
    return normalize(text).lower() in CANCEL_WORDS
