"""Card JSON 2.0 rendering; CoreMan interaction keys remain platform independent."""

from __future__ import annotations

import json
import re
from typing import Any

from coreman.core.chat import stream_stop
from coreman.core.feishu_cards.compile import card_shell, streaming_text
from coreman.core.feishu_cards.components import bubble
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.thinking import thinking_panel
from coreman.core.wecom.cards import clip

THINKING_BYTES = 4000
ANSWER_BYTES = 16000
_THINK = re.compile(r"<think>(.*?)</think>", re.S | re.I)
# Markdown 图片：远程地址飞书不渲染，只认上传后得到的 image_key。
REMOTE_IMAGE = re.compile(r'!\[([^\]\n]*)\]\(\s*<?(https?://[^\s<>()]+)>?(?:\s+"[^"\n]*")?\s*\)')
KEY_IMAGE = re.compile(r"!\[([^\]\n]*)\]\((img_[A-Za-z0-9_-]{1,240})\)")


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


def remote_images_as_links(markdown: str) -> str:
    """没换成 image_key 的远程图片改成链接，免得客户端显示一张裂图。"""
    return REMOTE_IMAGE.sub(lambda m: f"[🖼️ {m.group(1).strip() or '图片'}]({m.group(2)})", markdown)


def post_content(markdown: str) -> dict[str, Any]:
    """富文本消息：md 标签不支持图片，image_key 图片各占一段 img 节点。"""
    rows: list[list[dict[str, Any]]] = []
    last = 0
    for match in KEY_IMAGE.finditer(markdown):
        text = markdown[last : match.start()].strip("\n")
        if text.strip():
            rows.append([{"tag": "md", "text": remote_images_as_links(text)}])
        rows.append([{"tag": "img", "image_key": match.group(2)}])
        last = match.end()
    tail = markdown[last:].strip("\n")
    if tail.strip() or not rows:
        rows.append([{"tag": "md", "text": remote_images_as_links(tail) or "…"}])
    return {"zh_cn": {"title": "", "content": rows}}


# 打字速度约 50 字/秒：跟得上模型的输出，积压少，新增量到达时不会一下子跳出一大段。
# （飞书默认 70ms/1 字 ≈ 14 字/秒，远慢于模型，配合 fast 策略会一顿一顿地整段上屏。）
STREAMING_CONFIG: dict[str, Any] = {
    "print_frequency_ms": {"default": 40, "android": 40, "ios": 40, "pc": 40},
    "print_step": {"default": 2, "android": 2, "ios": 2, "pc": 2},
    "print_strategy": "fast",
}


# 「停止」按钮放在思考面板里（「查看完整思考过程」后面），见 thinking_panel。
STOP_BUTTON_ID = "stop_button"
# 飞书系统消息分割线的文字上限。
NEW_SESSION_LABEL_CHARS = 10


def stop_button(task_id: int) -> dict[str, Any]:
    """流式期间的「停止」按钮；收尾整卡替换成终稿时它就没了。"""
    return {
        "tag": "button",
        "element_id": STOP_BUTTON_ID,
        "type": "default",
        "size": "small",
        "text": {"tag": "plain_text", "content": "⏹ 停止"},
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "task_id": stream_stop.card_task_id(task_id),
                    "event_key": stream_stop.EVENT_KEY,
                },
            }
        ],
    }


def stream_card(
    thinking: str,
    answer: str,
    *,
    streaming: bool = True,
    session_url: str | None = None,
    heading: str = "🤔 思考过程",
    stop_task: int | None = None,
) -> dict[str, Any]:
    """流式卡片：思考面板（给了 `stop_task` 时里面带停止按钮）+ 一个 answer markdown。

    正文里的 card: 块先显示成可读的 Markdown、没写完的块显示占位，终稿时整卡换成富卡片；
    外壳与终稿一致（宽度、留白、配色），替换时不会跳动。
    """
    thinking, answer = visible_parts(thinking, answer)
    content = remote_images_as_links(split_utf8(streaming_text(answer))[0]) or "…"
    stop = stop_button(stop_task) if stop_task is not None and streaming else None
    elements = [
        thinking_panel(thinking, session_url=session_url, heading=heading, stop=stop),
        {"tag": "markdown", "element_id": "answer", "content": content},
    ]
    return card_shell(elements, streaming=streaming, streaming_config=STREAMING_CONFIG)


def new_session_card(label: str, hint: str) -> dict[str, Any]:
    """群里重置会话的回执：一张灰色的分隔小卡片（群里发不了系统分割线）。"""
    lines = f"—— {label} ——" + (f"\n{hint}" if hint else "")
    return card_shell(
        [
            {
                "tag": "markdown",
                "content": f"<font color='grey'>{lines}</font>",
                "text_align": "center",
                "text_size": "notation",
            }
        ],
        summary=label,
    )


# 单选题选项不多时一排按钮点一下就提交，不用「下拉框 + 提交」两步；多选、选项多的仍用表单。
ONE_TAP_OPTIONS = range(2, 5)
# 按钮文字的上限（飞书是 100 字）：选择题选项本来就截到 11 字，限流切换卡的实例名不截，
# 在这里兜底；完整的名字在卡片正文里。
BUTTON_TEXT_CHARS = 40


def _one_tap(checkbox: dict[str, Any], options: list[Any]) -> bool:
    return checkbox.get("mode") != 1 and len(options) in ONE_TAP_OPTIONS


def _choice_buttons(
    card: dict[str, Any],
    checkbox: dict[str, Any],
    submit: dict[str, Any],
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    """每个选项一个按钮；value 带齐表单提交时回调里有的东西（task_id、event_key），外加
    题目 key 与选项 id，入站折成同样的 `selected`，选择流程不用区分按钮还是表单。"""
    base = {
        "task_id": str(card.get("task_id") or ""),
        "event_key": str(submit.get("key") or ""),
        "question": str(checkbox.get("question_key") or "choice_answer"),
    }
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "button",
                        "type": "default",
                        "size": "small",
                        "text": {
                            "tag": "plain_text",
                            "content": clip(str(option.get("text") or "…"), BUTTON_TEXT_CHARS),
                        },
                        "behaviors": [
                            {"type": "callback", "value": {**base, "option": str(option["id"])}}
                        ],
                    }
                ],
            }
            for option in options
        ],
    }


def _choice_bubbles(
    card: dict[str, Any],
    checkbox: dict[str, Any],
    submit: dict[str, Any],
    options: list[dict[str, Any]],
    rich: list[Any],
) -> list[dict[str, Any]]:
    """飞书的单选题：每个选项一个整句换行的气泡，下面一行灰字说明，推荐项蓝框加「推荐」标签。

    回调 value 与按钮版一样（task_id、event_key、题目 key、选项 id）。「其他」放在最后。
    """
    base = {
        "task_id": str(card.get("task_id") or ""),
        "event_key": str(submit.get("key") or ""),
        "question": str(checkbox.get("question_key") or "choice_answer"),
    }
    ctx = RenderContext()
    details = {str(o.get("id")): o for o in rich if isinstance(o, dict)}
    out: list[dict[str, Any]] = []
    for option in options:
        oid = str(option["id"])
        detail = details.get(oid) or {}
        out.append(
            bubble(
                ctx,
                str(detail.get("text") or option.get("text") or "…"),
                {**base, "option": oid},
                recommended=bool(detail.get("recommended")),
                note=str(detail.get("description") or ""),
            )
        )
    return out


def interaction_card(card: dict[str, Any]) -> dict[str, Any]:
    if card.get("schema") == "2.0":
        return {key: value for key, value in card.items() if key != "task_id"}
    title = card.get("main_title") or {}
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": str(title.get("desc") or title.get("title") or "…")},
    ]
    if card.get("sub_title_text"):
        elements.append({"tag": "markdown", "content": str(card["sub_title_text"])})
    checkbox = card.get("checkbox") or {}
    submit = card.get("submit_button") or {}
    options = checkbox.get("option_list") or []
    rich = card.get("rich_options")
    if checkbox and not checkbox.get("disable") and submit and _one_tap(checkbox, options):
        if isinstance(rich, list) and rich:
            elements.extend(_choice_bubbles(card, checkbox, submit, options, rich))
        else:
            elements.append(_choice_buttons(card, checkbox, submit, options))
    elif checkbox and not checkbox.get("disable") and submit:
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


def media_card(kind: str, file_key: str, name: str) -> dict[str, Any]:
    """一段视频或音频放进一张单独的卡片直接播放（这两种组件要求卡片不能转发）。"""
    fallback = {"tag": "fallback_text", "text": {"tag": "plain_text", "content": f"📎 {name}"}}
    player: dict[str, Any] = (
        {"tag": "video", "element_id": "media", "file_key": file_key, "fallback": fallback}
        if kind == "video"
        else {
            "tag": "audio",
            "element_id": "media",
            "audio_id": "media",
            "file_key": file_key,
            "width": "fill",
            "time_display": "both",
            "time_position": "both_sides",
            "fallback": fallback,
        }
    )
    card = card_shell(
        [
            {
                "tag": "markdown",
                "content": f"<font color='grey'>📎 {name}</font>",
                "text_size": "notation",
            },
            player,
        ],
        summary=f"📎 {name}",
    )
    card["config"]["enable_forward"] = False
    return card
