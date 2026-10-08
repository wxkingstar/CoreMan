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
# Platform-written labels that carry the conversation tag, e.g. `[SYS_USER:<tag>]`,
# `[SYS_TURN:<tag>]` and `[/SYS_TURN:<tag>]`. The tag lives for the whole conversation, so
# message text may not carry a tagged line at all, guessed or leaked.
_TAGGED = frozenset({"sysuser", "systurn"})


def _impersonates(label: str) -> bool:
    key = _SEPARATORS.sub("", label)
    return key in _LABELS or key.split(":", 1)[0].lstrip("/") in _TAGGED


def sanitize_user_input(text: str) -> str:
    """Preserve ordinary text, dropping lines that impersonate system identity."""
    visible = "".join(
        char for char in text if unicodedata.category(char) != "Cf" and char != "\u034f"
    )
    result = []
    for line in visible.split("\n"):
        normalized = unicodedata.normalize("NFKC", line).casefold()
        if not any(_impersonates(label) for label in _BRACKETS.findall(normalized)):
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
