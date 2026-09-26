"""飞书卡片的视觉约定：配色、字号、圆角、图标，一处定义，所有渲染器共用。

深浅两种主题都要好看，规则是：
- 文字与图标只用颜色枚举里不带数字的色相名（等于 600 档，深色主题下飞书自动换成亮色）；
- 底色只用两类：中性面板用成对声明的半透明自定义色，语义提示条用 `-50` 底配 `-200` 边框
  （数字色阶在深色主题下会反转，仍然成立）；
- 不写只适合一种主题的颜色；自定义颜色必须 light / dark 成对且两值不同（有单元测试守着）。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------- 卡片级

PANEL = "cus-panel"
# 半透明：浅色主题比底色略深、深色主题比底色略浅，与卡片底色无关（真机验证过）。
CUSTOM_COLORS: dict[str, dict[str, str]] = {
    PANEL: {"light_mode": "rgba(31,35,41,0.04)", "dark_mode": "rgba(255,255,255,0.06)"},
}
BODY_PADDING = "14px 16px 16px 16px"
BODY_SPACING = "10px"
RADIUS = "8px"
PANEL_PADDING = "12px 14px 12px 14px"
TILE_PADDING = "10px 12px 10px 12px"

# 字号四档：KPI 数字、小节标题、正文、注释。
TEXT_KPI = "heading"
TEXT_TITLE = "heading-4"
TEXT_BODY = "normal"
TEXT_NOTE = "notation"

# ---------------------------------------------------------------- 语义色

Level = Literal["info", "success", "warning", "danger"]
# 文字色（600 档色相名）、提示条底色、边框色、图标。
LEVELS: dict[str, dict[str, str]] = {
    "info": {"text": "blue", "bg": "blue-50", "border": "blue-200", "icon": "info_outlined"},
    "success": {
        "text": "green",
        "bg": "green-50",
        "border": "green-200",
        "icon": "succeed_filled",
    },
    "warning": {
        "text": "orange",
        "bg": "orange-50",
        "border": "orange-200",
        "icon": "warning_outlined",
    },
    "danger": {"text": "red", "bg": "red-50", "border": "red-200", "icon": "error_filled"},
}
# 时间线步骤状态 → (图标, 颜色)。
STATUS: dict[str, tuple[str, str]] = {
    "done": ("yes_outlined", "green"),
    "current": ("time_outlined", "blue"),
    "warning": ("warning_outlined", "orange"),
    "error": ("close_outlined", "red"),
    "pending": ("maybe_outlined", "grey"),
}
GOOD, BAD, NEUTRAL = "green", "red", "grey"

# ---------------------------------------------------------------- 图表

# 图表不能引用卡片自定义色，只能写固定色值，所以数据色取中明度：白底和深色底上都清楚。
CHART_PALETTE: tuple[str, ...] = (
    "#3370EB",  # 蓝
    "#169C89",  # 青
    "#ED6D0C",  # 橙
    "#8D55ED",  # 紫
    "#D99904",  # 黄
    "#1295CA",  # 天蓝
    "#CF5ECF",  # 紫红
    "#F54A45",  # 红
)
# 排行、占比类单系列：同色系由深到浅，最后一档留给「其他」。
CHART_RAMP: tuple[str, ...] = ("#169C89", "#3BB7A3", "#6FCDBD", "#A3E0D5", "#C4EDE5")
CHART_OTHER = "rgba(143,149,158,0.55)"
CHART_SEMANTIC: dict[str, str] = {
    "blue": "#3370EB",
    "teal": "#169C89",
    "green": "#32A645",
    "orange": "#ED6D0C",
    "purple": "#8D55ED",
    "yellow": "#D99904",
    "red": "#F54A45",
    "grey": "#8F959E",
    # 好坏语义：涨跌的颜色按好坏定，不按方向定。
    "good": "#32A645",
    "bad": "#F54A45",
}
CHART_HIGHLIGHT = "#ED6D0C"
# 标签颜色写进 chart_spec.theme，用语义色解析，才会跟着深浅主题切换（真机验证过）。
LABEL_PRIMARY: dict[str, Any] = {"type": "palette", "key": "primaryFontColor"}
LABEL_SECONDARY: dict[str, Any] = {"type": "palette", "key": "axisLabelFontColor"}
# 全宽与半宽下的图表高度（px）。
CHART_HEIGHT = {"sm": 160, "md": 240, "lg": 320}
CHART_HALF_HEIGHT = 180

# ---------------------------------------------------------------- 标题栏与标签

HEADER_TEMPLATES: frozenset[str] = frozenset(
    {
        "blue",
        "wathet",
        "turquoise",
        "green",
        "yellow",
        "orange",
        "red",
        "carmine",
        "violet",
        "purple",
        "indigo",
        "grey",
        "default",
    }
)
TAG_COLORS: frozenset[str] = frozenset(
    {
        "neutral",
        "blue",
        "turquoise",
        "lime",
        "orange",
        "violet",
        "indigo",
        "wathet",
        "green",
        "yellow",
        "red",
        "purple",
        "carmine",
    }
)
# markdown `<font color>` 允许的色相名（600 档，自动适配深浅主题）。
FONT_COLORS: frozenset[str] = frozenset(
    {"green", "red", "orange", "blue", "grey", "yellow", "purple", "turquoise", "default"}
)


@cache
def icon_tokens() -> frozenset[str]:
    """飞书图标库 token 白名单。outlined 与 filled 不对称，不能靠换后缀猜。"""
    path = Path(__file__).with_name("icons.txt")
    return frozenset(line.strip() for line in path.read_text("utf-8").splitlines() if line.strip())


def icon(token: str | None, color: str | None = None) -> dict[str, Any] | None:
    """标准图标；token 不在白名单里返回 None（调用方直接不加图标）。"""
    if not token or token not in icon_tokens():
        return None
    out: dict[str, Any] = {"tag": "standard_icon", "token": token}
    if color:
        out["color"] = color
    return out


def card_style() -> dict[str, Any]:
    return {"color": {k: dict(v) for k, v in CUSTOM_COLORS.items()}}
