"""流式卡片上的「停止」按钮：提问人点一下，停掉正在生成的这条回复（等同于他发了「停止」，
但只停这一条）。

按钮回调 value 的 task_id 是 `stop@<这一轮的任务 id>`。群里只有提问人能停，别人点了不算；
worker 还要核对点的是不是这条任务自己的卡片。
"""

from __future__ import annotations

import re
from typing import Any

CARD_PREFIX = "stop@"
EVENT_KEY = "stream_stop"
# 写在后台阶段进程里的一轮：点「停止」先弹确认（会连带停掉后台任务），确认卡上按钮的 level。
CONFIRMED = "confirmed"
_TASK_ID = re.compile(r"stop@([1-9][0-9]{0,18})")


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


def confirm_card(task_id: int, running: int, locale: str = "zh") -> dict[str, Any]:
    """停下写在后台阶段进程里的一轮之前的确认：停下会连带停掉所有后台任务。不点就不停。"""
    from coreman.core.i18n.messages import msg

    return {
        "schema": "2.0",
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": msg("live_stop_confirm_title", locale)},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": msg("live_stop_confirm_desc", locale, n=running),
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": msg("live_stop_confirm_ok", locale)},
                    "type": "danger",
                    "width": "fill",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "task_id": card_task_id(task_id),
                                "event_key": EVENT_KEY,
                                "level": CONFIRMED,
                            },
                        }
                    ],
                },
            ]
        },
        "task_id": card_task_id(task_id),
    }
