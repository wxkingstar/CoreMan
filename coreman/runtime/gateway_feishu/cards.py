"""Card JSON 2.0 rendering; CoreMan interaction keys remain platform independent."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

THINKING_BYTES = 4000
ANSWER_BYTES = 16000
_THINK = re.compile(r"<think>(.*?)</think>", re.S | re.I)


def split_utf8(value: str, limit: int = ANSWER_BYTES) -> list[str]:
    if limit < 4:
        raise ValueError("UTF-8 chunk budget too small")
    result: list[str] = []
    chunk: list[str] = []
    size = 0
    for char in value:
        cost = len(json.dumps(char, ensure_ascii=False)[1:-1].encode())
        if chunk and size + cost > limit:
            result.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += cost
    if chunk:
        result.append("".join(chunk))
    return result or [""]


def visible_parts(thinking: str, answer: str) -> tuple[str, str]:
    hidden = _THINK.findall(answer)
    answer = _THINK.sub("", answer)
    # A streaming <think> block may be incomplete; it is never answer content.
    opened = re.search(r"<think>(.*)$", answer, re.S | re.I)
    if opened:
        hidden.append(opened.group(1))
        answer = answer[: opened.start()]
    for length in range(1, len("<think>")):
        if answer.lower().endswith("<think>"[:length]):
            answer = answer[:-length]
            break
    thinking = "\n".join([thinking, *hidden]).strip()
    return thinking, answer


def thinking_preview(thinking: str) -> str:
    # Five logical lines; client wrapping depends on its viewport and font size.
    lines = [line for line in thinking.splitlines() if line.strip()]
    return "\n".join(line[-240:] for line in lines[-5:]) or "正在思考，请稍候…"


def stream_card(
    thinking: str,
    answer: str,
    *,
    streaming: bool = True,
    session_url: str | None = None,
    heading: str = "🤔 思考过程",
) -> dict[str, Any]:
    thinking, answer = visible_parts(thinking, answer)
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": streaming,
            "update_multi": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 70, "android": 70, "ios": 70, "pc": 70},
                "print_step": {"default": 1, "android": 1, "ios": 1, "pc": 1},
                "print_strategy": "fast",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "collapsible_panel",
                    "element_id": "thinking_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "plain_text", "content": heading}},
                    "background_color": "grey",
                    "border": {"color": "grey", "corner_radius": "8px"},
                    "padding": "8px",
                    "elements": [
                        {
                            "tag": "markdown",
                            "element_id": "thinking",
                            "content": thinking_preview(thinking),
                            "text_size": "notation",
                        }
                    ],
                },
                {
                    "tag": "markdown",
                    "element_id": "answer",
                    "content": split_utf8(answer)[0] or "…",
                },
            ]
        },
    }
    if session_url and urlparse(session_url).scheme in {"http", "https"}:
        # Use a native URL button; do not interpolate the URL into model Markdown.
        card["body"]["elements"][0]["elements"].append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看完整思考过程"},
                "type": "default",
                "behaviors": [{"type": "open_url", "default_url": session_url}],
            }
        )
    return card


def interaction_card(card: dict[str, Any]) -> dict[str, Any]:
    if card.get("schema") == "2.0":
        return card
    title = card.get("main_title") or {}
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": str(title.get("desc") or title.get("title") or "…")},
    ]
    if card.get("sub_title_text"):
        elements.append({"tag": "markdown", "content": str(card["sub_title_text"])})
    checkbox = card.get("checkbox") or {}
    submit = card.get("submit_button") or {}
    if checkbox and not checkbox.get("disable") and submit:
        elements.append(
            {
                "tag": "form",
                "name": "choice_form",
                "elements": [
                    {
                        "tag": "multi_select_static"
                        if checkbox.get("mode") == 1
                        else "select_static",
                        "name": str(checkbox.get("question_key") or "choice_answer"),
                        "options": [
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": str(option.get("text") or "…"),
                                },
                                "value": str(option["id"]),
                            }
                            for option in checkbox.get("option_list") or []
                        ],
                    },
                    {
                        "tag": "button",
                        "name": "submit",
                        "form_action_type": "submit",
                        "text": {"tag": "plain_text", "content": str(submit.get("text") or "✓")},
                        "type": "primary",
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {
                                    "task_id": str(card.get("task_id") or ""),
                                    "event_key": str(submit.get("key") or ""),
                                },
                            }
                        ],
                    },
                ],
            }
        )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": str(title.get("title") or "CoreMan")[:100]}
        },
        "body": {"elements": elements},
    }
