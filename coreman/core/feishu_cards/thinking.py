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
) -> dict[str, Any]:
    """折叠的「思考过程」面板：流式卡片和终稿卡片的第一个组件都是它。"""
    panel: dict[str, Any] = {
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
    }
    if session_url and urlparse(session_url).scheme in {"http", "https"}:
        # Use a native URL button; do not interpolate the URL into model Markdown.
        panel["elements"].append(
            {
                "tag": "button",
                "element_id": "session_link",
                "text": {"tag": "plain_text", "content": "查看完整思考过程"},
                "type": "default",
                "behaviors": [{"type": "open_url", "default_url": session_url}],
            }
        )
    return panel
