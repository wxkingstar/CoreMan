"""「思考过程」折叠面板：流式卡片和终稿卡片的第一个组件都是它。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def thinking_preview(thinking: str) -> str:
    # Five logical lines; client wrapping depends on its viewport and font size.
    lines = [line for line in thinking.splitlines() if line.strip()]
    return "\n".join(line[-240:] for line in lines[-5:]) or "正在思考，请稍候…"


def thinking_panel(
    thinking: str,
    *,
    session_url: str | None = None,
    heading: str = "🤔 思考过程",
    stop: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """折叠的「思考过程」面板：流式卡片和终稿卡片的第一个组件都是它。

    `stop` 是流式期间的「停止」按钮：停止很少用，放在面板里「查看完整思考过程」后面，
    不占正文下方的视线；收尾整卡替换成终稿时它就没了。
    """
    panel: dict[str, Any] = {
        "tag": "collapsible_panel",
        "element_id": "thinking_panel",
        "expanded": False,
        # markdown：运行中的状态行用 <local_datetime> 按看的人设备时区显示时间。
        "header": {"title": {"tag": "markdown", "content": heading}},
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
    }
    if heading.startswith("❌"):
        # 中断了：边框标红，一眼看得出不是正常结束。
        panel["border"]["color"] = "red"
    buttons = []
    if session_url and urlparse(session_url).scheme in {"http", "https"}:
        # Use a native URL button; do not interpolate the URL into model Markdown.
        buttons.append(
            {
                "tag": "button",
                "element_id": "session_link",
                "text": {"tag": "plain_text", "content": "查看完整思考过程"},
                "type": "default",
                # 与「停止」按钮同为 small，两个并排时高度一致。
                "size": "small",
                "behaviors": [{"type": "open_url", "default_url": session_url}],
            }
        )
    if stop is not None:
        buttons.append(stop)
    if len(buttons) > 1:
        panel["elements"].append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [
                    {"tag": "column", "width": "auto", "elements": [button]} for button in buttons
                ],
            }
        )
    else:
        panel["elements"].extend(buttons)
    return panel
