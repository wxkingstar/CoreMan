"""出站密钥闸门：本轮注入的凭据不得出现在用户可见内容或对话记录里。

注入 CLI 的 `BOT_TOKEN_<系统>` 是一枚活着的发言者凭据（最长 12 小时）。模型只要在回复里
复述一次——「我用的令牌是 eyJ...」「curl -H 'Cookie: bot_token=…'」——它就同时进了 IM
聊天记录和 `chat_logs`，而那两处的可见范围都远大于「当前发言者」。提示词已经要求不要这么
做，但提示词不是边界，所以在投递前再过一道。

两条规则：

1. **本轮凭据的字面量**：从 env 里挑出凭据类键的值，逐个替换。这条零误伤——被替换掉的
   就是这次真的发出去过的密钥。
2. **JWT 形状**：`eyJ` 开头的三段式串一律替换。这条会误伤（用户请模型解释一条 JWT 时也会
   被打码），但 CoreMan 的令牌都是 JWT，而模型完全可能把它重新编码、拆行后再打印出来，
   只比对字面量挡不住。安全优先于这个边缘场景。

只改「给人看的东西」：发给模型的提示词、工具参数、运行时请求体都不经过这里——那些地方
本来就得带着真凭据才能干活。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from coreman.core.prompting.env_vars import RESERVED_KEYS, SPEAKER_TOKEN_PREFIX

PLACEHOLDER = "[已屏蔽的凭据]"
# 短值不替换：8 位以下的「密钥」多半是占位符或开关，替换它只会把正文打得七零八落。
MIN_SECRET_LENGTH = 8
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}")
_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "cookie",
    "session_key",
)


def is_secret_key(key: str) -> bool:
    """这个 env 键的值算不算凭据。

    `RESERVED_KEYS` 一律不算：那是身份与业务系统配置（`COREMAN_BOT_KEY`、`BOT_KEY` 等，
    名字里带 KEY 但只是标识符），把它们当密钥替换会把正文里的人名、会话号全打成方块。
    """
    if key in RESERVED_KEYS:
        return False
    if key.startswith(SPEAKER_TOKEN_PREFIX):
        return True
    lowered = key.lower()
    return any(marker in lowered for marker in _KEY_MARKERS)


def collect_secrets(env: dict[str, str]) -> frozenset[str]:
    """本轮 env 里需要拦在出站方向上的值。"""
    return frozenset(
        value.strip()
        for key, value in env.items()
        if is_secret_key(key) and len(value.strip()) >= MIN_SECRET_LENGTH
    )


@dataclass(frozen=True)
class Redacted:
    """替换后的正文，以及把原文下标换算成新下标的能力。

    `task_streams.segment_boundaries` 是 `pending_text` 上的下标，替换会改变串长，
    不跟着挪的话切分点就会落错位置。
    """

    text: str
    # (原文中该替换区间的结束下标, 此处之前累计的长度变化)，按下标递增。
    _shifts: tuple[tuple[int, int], ...] = ()

    def shift(self, index: int) -> int:
        """原文下标 → 新下标。落在被替换区间内的下标归到占位符的起点。"""
        moved = index
        for end, delta in self._shifts:
            if end <= index:
                moved = index + delta
            else:
                break
        return max(0, min(moved, len(self.text)))


def _spans(text: str, secrets: frozenset[str]) -> list[tuple[int, int]]:
    """所有要替换的区间，按起点排序并合并重叠部分。"""
    found: list[tuple[int, int]] = [m.span() for m in _JWT.finditer(text)]
    for secret in secrets:
        start = text.find(secret)
        while start != -1:
            found.append((start, start + len(secret)))
            start = text.find(secret, start + len(secret))
    found.sort()
    merged: list[tuple[int, int]] = []
    for start, end in found:
        # 重叠或相接（一个密钥是另一个的子串、JWT 与字面量命中同一段）合成一段，
        # 否则会在同一处连着放两个占位符。
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def redact_text(text: str, secrets: frozenset[str]) -> Redacted:
    """替换本轮凭据与 JWT 形状的串，并保留下标换算。"""
    spans = _spans(text, secrets)
    if not spans:
        return Redacted(text)
    out: list[str] = []
    shifts: list[tuple[int, int]] = []
    cursor = delta = 0
    for start, end in spans:
        out.append(text[cursor:start])
        out.append(PLACEHOLDER)
        delta += len(PLACEHOLDER) - (end - start)
        shifts.append((end, delta))
        cursor = end
    out.append(text[cursor:])
    return Redacted("".join(out), tuple(shifts))


def redact(text: str | None, secrets: frozenset[str]) -> str | None:
    """`redact_text` 的简写；`None` 与空串原样返回。"""
    if not text:
        return text
    return redact_text(text, secrets).text


def redact_value(value: Any, secrets: frozenset[str]) -> Any:
    """递归过滤结构里的字符串值，键名不动（键名不是模型写的正文）。

    给会被持久化、之后再渲染给用户看的模型产物用，例如问答卡片的题面与选项。
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, secrets) for key, item in value.items()}
    return value
