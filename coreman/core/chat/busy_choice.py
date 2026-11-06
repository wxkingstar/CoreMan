"""上一条还在处理时又来了新消息：新消息先排队，卡片让发消息的人选「停下上一条」还是「等它做完」。

不点也行：新消息本来就排在后面，上一条做完自动接着处理；只有点「停下」才会动正在跑的任务。
「看看进度」只读上一条的实时状态回一句，卡片和按钮都留着，可以再点。
卡片的 task_id 是 `busy@<新消息的任务 id>`，飞书与企微共用。
"""

from __future__ import annotations

import re
from typing import Any

from coreman.core.i18n.messages import msg

CARD_PREFIX = "busy@"
STOP = "busy_stop"
WAIT = "busy_wait"
PROGRESS = "busy_progress"
_TASK_ID = re.compile(r"busy@([1-9][0-9]{0,18})")


def card_task_id(task_id: int) -> str:
    return f"{CARD_PREFIX}{task_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


def offer_card(
    platform: str, task_id: int, *, progress: str = "", locale: str = "zh"
) -> dict[str, Any]:
    """`progress` 是点过「看看进度」后的进度说明，放在问句前面；按钮照旧。"""
    tid = card_task_id(task_id)
    if platform == "wecom":
        sub = msg("busy_default", locale)
        return {
            "card_type": "button_interaction",
            "source": {"desc": msg("card_source_ai", locale)},
            "main_title": {
                "title": msg("busy_title", locale),
                "desc": msg("busy_ask", locale),
            },
            "sub_title_text": f"{progress}\n{sub}" if progress else sub,
            "button_list": [
                {"text": msg("busy_opt_stop", locale), "style": 1, "key": STOP},
                {"text": msg("busy_opt_wait", locale), "style": 2, "key": WAIT},
                {"text": msg("busy_opt_progress", locale), "style": 2, "key": PROGRESS},
            ],
            "task_id": tid,
        }

    def button(key: str, label: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": msg(label, locale)},
            "type": kind,
            "behaviors": [{"type": "callback", "value": {"task_id": tid, "event_key": key}}],
        }

    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": msg("busy_title", locale)},
        },
        "body": {
            "elements": [
                *([{"tag": "markdown", "content": progress}] if progress else []),
                {
                    "tag": "markdown",
                    "content": msg("busy_ask", locale)
                    + "\n<font color='grey'>"
                    + msg("busy_default", locale)
                    + "</font>",
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [button(STOP, "busy_opt_stop", "primary")],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [button(WAIT, "busy_opt_wait", "default")],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [button(PROGRESS, "busy_opt_progress", "default")],
                        },
                    ],
                },
            ]
        },
        "task_id": tid,
    }


def result_card(
    platform: str, task_id: str, *, title: str, desc: str, icon_url: str = "", locale: str = "zh"
) -> dict[str, Any]:
    """点过之后把卡片换成这张：按钮没了，只留结果。"""
    if platform == "wecom":
        from coreman.core.wecom.cards import notice_card

        return notice_card(
            task_id,
            title=title,
            desc=desc,
            icon_url=icon_url,
            source_desc=msg("card_source_ai", locale),
            locale=locale,
        )
    return {
        "schema": "2.0",
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": desc}]},
        "task_id": task_id,
    }


def _elapsed(seconds: float, locale: str) -> str:
    total = max(int(seconds), 0)
    minutes, secs = divmod(total, 60)
    if minutes:
        return msg("busy_elapsed_min", locale, m=minutes, s=secs)
    return msg("busy_elapsed_sec", locale, s=secs)


def progress_text(
    *, status: str, seconds: float, thinking_md: str, reply_text: str, locale: str = "zh"
) -> str:
    """上一条的进度：用了多久、做了几步、最近写到哪句。只读实时状态，不调用模型。"""
    if status == "queued":
        return msg("busy_progress_queued", locale)
    steps = thinking_md.count("🔧")
    text = msg("busy_progress_elapsed", locale, elapsed=_elapsed(seconds, locale))
    if steps:
        text += msg("busy_progress_steps", locale, steps=steps)
    text += "。"
    lines = [line.strip() for line in reply_text.splitlines() if line.strip()]
    if lines:
        recent = lines[-1].lstrip("#>*- ").strip()
        if len(recent) > 50:
            recent = recent[:49] + "…"
        text += "\n" + msg("busy_progress_recent", locale, recent=recent)
    return text
