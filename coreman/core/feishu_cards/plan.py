"""任务清单面板：agent 自己拆的步骤，一项一个勾选器，上面一条进度条。

勾选器一律禁用（AI 员工完成后自动勾上，人点不了）；进行中的那项蓝字。流式卡片和终稿
卡片都放在思考面板下面。进度条图表的配色遵守深浅两种主题（见 charts.py 的约定）：
不写文字颜色、底轨用半透明中性灰。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

PLAN_BOX_ID = "plan_box"
BAR_COLOR = "#169C89"
TRACK_COLOR = "rgba(128,128,128,0.18)"


def _bar(done: int, total: int) -> dict[str, Any]:
    return {
        "type": "linearProgress",
        "background": "transparent",
        "padding": {"top": 2, "right": 4, "bottom": 2, "left": 0},
        "data": {"values": [{"type": "progress", "value": done / total}]},
        "direction": "horizontal",
        "xField": "value",
        "yField": "type",
        "seriesField": "type",
        "color": [BAR_COLOR],
        "bandWidth": 8,
        "cornerRadius": 4,
        "progress": {"style": {"lineWidth": 0}},
        "track": {"style": {"fill": TRACK_COLOR}},
        "axes": [{"orient": "left", "type": "band", "visible": False}],
        "legends": {"visible": False},
        "tooltip": {"visible": False},
    }


def _checker(index: int, text: str, status: str) -> dict[str, Any]:
    return {
        "tag": "checker",
        "element_id": f"plan_step_{index}",
        "checked": status == "completed",
        "text": {
            "tag": "plain_text",
            "content": text,
            "text_color": "blue" if status == "in_progress" else "default",
        },
        "overall_checkable": False,
        "checked_style": {"show_strikethrough": False, "opacity": 0.55},
        "disabled": True,
        "disabled_tips": {"tag": "plain_text", "content": "由 AI 员工完成后自动勾选"},
    }


def plan_box(plan: Sequence[dict[str, str]]) -> dict[str, Any] | None:
    """任务清单面板；清单为空时返回 None。"""
    items = [
        (str(item.get("text") or ""), str(item.get("status") or ""))
        for item in plan
        if item.get("text")
    ]
    if not items:
        return None
    done = sum(status == "completed" for _, status in items)
    return {
        "tag": "interactive_container",
        "element_id": PLAN_BOX_ID,
        "background_style": "grey",
        "corner_radius": "8px",
        "padding": "10px 12px 10px 12px",
        "vertical_spacing": "4px",
        "behaviors": [],
        "elements": [
            {
                "tag": "markdown",
                "element_id": "plan_title",
                "content": f"**任务清单** {done}/{len(items)}",
            },
            {
                "tag": "chart",
                "element_id": "plan_bar",
                "chart_spec": _bar(done, len(items)),
                "height": "28px",
                "preview": False,
            },
            *(_checker(i, text, status) for i, (text, status) in enumerate(items)),
        ],
    }
