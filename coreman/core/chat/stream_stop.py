"""流式卡片上的「停止」按钮：提问人点一下，停掉正在生成的这条回复（等同于他发了「停止」，
但只停这一条）。

按钮回调 value 的 task_id 是 `stop@<这一轮的任务 id>`。群里只有提问人能停，别人点了不算；
worker 还要核对点的是不是这条任务自己的卡片。
"""

from __future__ import annotations

import re

CARD_PREFIX = "stop@"
EVENT_KEY = "stream_stop"
_TASK_ID = re.compile(r"stop@([1-9][0-9]{0,18})")


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None
