"""Filter reserved identity labels from untrusted message text."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_LABELS = frozenset(
    {
        "sysuser",
        "currentuser",
        "systemuser",
        "agentuser",
        "realuser",
        "authuser",
        "当前用户",
        "系统用户",
        "当前发言者",
        "操作者身份",
        "ユーザー",
        "現在のユーザー",
    }
)
_BRACKETS = re.compile(r"\[([^\[\]\n]*)\]")
_SEPARATORS = re.compile(r"[\s_.\-]+")


def sanitize_user_input(text: str) -> str:
    """Preserve ordinary text, dropping lines that impersonate system identity."""
    visible = "".join(
        char for char in text if unicodedata.category(char) != "Cf" and char != "\u034f"
    )
    result = []
    for line in visible.split("\n"):
        normalized = unicodedata.normalize("NFKC", line).casefold()
        labels = _BRACKETS.findall(normalized)
        if not any(_SEPARATORS.sub("", label) in _LABELS for label in labels):
            result.append(line)
    return "\n".join(result)


def sanitize_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy content parts; never interpret binary attachment payloads as text."""
    result = []
    for part in parts:
        item = part.copy()
        if item.get("type") == "text":
            item["text"] = sanitize_user_input(str(item.get("text") or ""))
        result.append(item)
    return result
