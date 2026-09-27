"""飞书图表：把图表 DSL（ChartBlock）编译成 VChart spec 和 chart 组件。

模型只描述数据和意图，这里负责清洗数据、控制数据量、预先格式化，再套固定模板。所有模板
共用一套「深浅双主题安全」的基底（真机的浅色、深色主题下都验证过）：
- 文字、坐标轴、网格、图例、tooltip、底轨一律不写颜色，交给飞书主题；底轨只写圆角；
- `background: "transparent"`；label 和 point 一律 `lineWidth: 0`，去掉背景色光晕；
- 关闭 hover / select，去掉交互后留下的描边；
- 数据标签的颜色写进 `spec.theme`，用语义色解析，才会跟着主题切换；
- 只用 VChart 1.10 就有的特性（飞书 7.20–7.26 客户端），不写函数、字体、纹理和圆锥渐变。

数据标签尽量由这里预先算成文本字段 `t`，模板只写 `"{t}"`，少依赖 d3-format。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.richtext.schema import ChartBlock, NumberFormat

# ---------------------------------------------------------------- 数据量上限

# 折线、面积每个系列最多多少点（超出均匀降采样）；半宽减半。
LINE_MAX_POINTS = 60
HALF_LINE_MAX_POINTS = 30
# 柱状最多多少个类目（超出截断）；半宽减半。
BAR_MAX_CATEGORIES = 24
HALF_BAR_MAX_CATEGORIES = 12
# 单个 spec 的体积预算（字节，按投递时的 json.dumps 口径）：超了就按 _SHRINK 逐级减少数据点重算。
SPEC_BUDGET = 6000
_SHRINK = (1.0, 0.7, 0.5, 0.35, 0.25, 0.18, 0.12, 0.08, 0.05)
PIE_TOP_N = 6
HBAR_TOP_N = 10
FUNNEL_MAX = 8
PROGRESS_MAX = 10
RADAR_MAX_DIMS = 12
SCATTER_MAX_POINTS = 120
# 柱子不超过这么多根时才默认显示数值标签。
BAR_LABEL_MAX = 8
# 折线点数不超过这么多时画出数据点。
LINE_POINT_MAX = 12

# ---------------------------------------------------------------- 尺寸与样式

TREND_HEIGHT = style.CHART_HEIGHT["md"]
ROUND_HEIGHT = 220  # 饼、环、雷达
ROW_HEIGHT = 32  # 排行条形、条形进度：每行高度
ROW_PADDING = 24
MAX_ROW_CHART_HEIGHT = 360
# 条形进度的底轨：VChart 主题里写死 #E7EBED，深色下是刺眼的亮条；改成半透明中性灰。
TRACK_FILL = "rgba(128,128,128,0.18)"
PADDING = {"top": 12, "right": 16, "bottom": 8, "left": 8}
HALF_PADDING = {"top": 8, "right": 8, "bottom": 4, "left": 4}
ROW_PADDING_SPEC = {"top": 4, "right": 8, "bottom": 4, "left": 4}
ROUND_PADDING = {"top": 8, "right": 8, "bottom": 8, "left": 8}

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB = re.compile(r"^rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*[\d.]+\s*)?\)$")
# 轴 formatter 是字符串模板，前缀和单位里不能混进花括号。
_BRACES = re.compile(r"[{}]")


# ---------------------------------------------------------------- 数值格式


def _compact(value: float) -> int | float:
    """写进 spec 的数值：最多两位小数（很小的数保留三位有效数字），整数不带 .0。"""
    if value == 0 or not math.isfinite(value):
        return 0
    rounded = round(value, 2) if abs(value) >= 1 else float(f"{value:.3g}")
    return int(rounded) if rounded.is_integer() else rounded


def _number_text(value: float, decimals: int | None) -> str:
    """千分位数字；没指定小数位时按量级取，并去掉尾零。"""
    if decimals is not None:
        return f"{value:,.{decimals}f}"
    size = abs(value)
    places = 0 if size >= 100 or float(value).is_integer() else (1 if size >= 10 else 2)
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _axis_decimals(largest: float, decimals: int | None) -> int:
    """轴刻度的小数位：按数据量级估计刻度步长（tickCount 4），用户给的小数位只会让它更少。"""
    guess = 0 if largest >= 5 else (1 if largest >= 0.5 else 2)
    return guess if decimals is None else min(decimals, guess)


@dataclass(frozen=True)
class _Format:
    """一个数值轴的格式：数据预先乘以 factor（万、亿、比值转百分数），文本拼上前缀和后缀。"""

    factor: float = 1.0
    prefix: str = ""
    suffix: str = ""
    decimals: int | None = None

    def value(self, raw: float) -> int | float:
        return _compact(raw * self.factor)

    def text(self, raw: float) -> str:
        return f"{self.prefix}{_number_text(raw * self.factor, self.decimals)}{self.suffix}"

    def axis(self, raws: Iterable[float]) -> str:
        largest = max((abs(v * self.factor) for v in raws), default=0.0)
        places = _axis_decimals(largest, self.decimals)
        return f"{self.prefix}{{label:,.{places}f}}{self.suffix}"


def _format(fmt: NumberFormat | None, raws: Iterable[float]) -> _Format:
    """解析 format：percent_input 优先；scale=auto 时最大值 ≥1e8 用亿、≥1e5 用万。"""
    fmt = fmt or NumberFormat()
    prefix = _BRACES.sub("", fmt.prefix or "")
    unit = _BRACES.sub("", fmt.unit or "")
    if fmt.percent_input is not None:
        factor = 100.0 if fmt.percent_input == "ratio" else 1.0
        return _Format(factor=factor, prefix=prefix, suffix="%", decimals=fmt.decimals)
    largest = max((abs(v) for v in raws), default=0.0)
    if fmt.scale == "yi" or (fmt.scale == "auto" and largest >= 1e8):
        return _Format(1e-8, prefix, "亿" + unit, fmt.decimals)
    if fmt.scale == "wan" or (fmt.scale == "auto" and largest >= 1e5):
        return _Format(1e-4, prefix, "万" + unit, fmt.decimals)
    return _Format(1.0, prefix, unit, fmt.decimals)


def _percent_text(ratio: float, decimals: int | None = None) -> str:
    """占比文本：0.321 → 32.1%，0.25 → 25%。"""
    if decimals is not None:
        return f"{ratio * 100:,.{decimals}f}%"
    return f"{ratio * 100:,.1f}".rstrip("0").rstrip(".") + "%"


# ---------------------------------------------------------------- 颜色


def _color(name: str | None) -> str | None:
    """语义名映射到双模安全色；写十六进制或 rgb(a) 的原样用；认不出的返回 None 走默认。"""
    if not name:
        return None
    text = name.strip()
    semantic = style.CHART_SEMANTIC.get(text.lower())
    if semantic:
        return semantic
    if _HEX.match(text) or _RGB.match(text):
        return text
    return None


def _palette_colors(block: ChartBlock, own: Sequence[str | None]) -> list[str]:
    """多系列数据色：系列自带 color 优先，其次 block.colors，最后按 CHART_PALETTE 轮换。"""
    wanted = block.colors or []
    out: list[str] = []
    for i, color in enumerate(own):
        picked = _color(color) or (_color(wanted[i]) if i < len(wanted) else None)
        out.append(picked or style.CHART_PALETTE[i % len(style.CHART_PALETTE)])
    return out


def _ramp_colors(
    block: ChartBlock, names: Sequence[str], own: Sequence[str | None], *, distinct: bool
) -> list[str]:
    """单系列占比数据色：同色系由深到浅，「其他」用中性灰。

    项数超过色阶档数时：排行条形按名次分档（相邻同色无妨）；饼图要求相邻可分，改用多色色板。
    """
    ramp = style.CHART_RAMP
    ranked = [n for n in names if n != block.other_name]
    use_palette = distinct and len(ranked) > len(ramp)
    wanted = block.colors or []
    out: list[str] = []
    for i, (name, color) in enumerate(zip(names, own, strict=True)):
        picked = _color(color) or (_color(wanted[i]) if i < len(wanted) else None)
        if picked:
            out.append(picked)
        elif name == block.other_name:
            out.append(style.CHART_OTHER)
        elif use_palette:
            out.append(style.CHART_PALETTE[i % len(style.CHART_PALETTE)])
        else:
            band = i if len(ranked) <= len(ramp) else i * len(ramp) // len(ranked)
            out.append(ramp[min(band, len(ramp) - 1)])
    return out


def _single_color(block: ChartBlock, own: str | None = None) -> str:
    """单一数据色（漏斗、进度、环形 KPI）：自带 color → block.colors[0] → 色阶首档。"""
    wanted = block.colors or []
    return _color(own) or (_color(wanted[0]) if wanted else None) or style.CHART_RAMP[0]


def _rgba(color: str, alpha: float) -> str:
    """十六进制色加透明度（面积渐变用）；不是 #RRGGBB 的原样返回。"""
    if _HEX.match(color) and len(color) == 7:
        r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return f"rgba({r},{g},{b},{alpha})"
    return color


# ---------------------------------------------------------------- 数据预处理


@dataclass(frozen=True)
class _Series:
    name: str
    values: list[float | None]
    color: str | None = None
    kind: str | None = None
    axis: str | None = None


@dataclass(frozen=True)
class _Item:
    name: str
    value: float
    color: str | None = None


def _unique(names: Iterable[str], fallback: str) -> list[str]:
    """系列名、类目名去重补齐：同名会被 seriesField 合成一个系列。"""
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(names):
        base = raw or f"{fallback}{i + 1}"
        name, n = base, 1
        while name in seen:
            n += 1
            name = f"{base}({n})"
        seen.add(name)
        out.append(name)
    return out


def _sample(indexes: int, keep: int) -> list[int]:
    """在 0..indexes-1 里均匀取 keep 个下标，首尾一定保留。"""
    if indexes <= keep:
        return list(range(indexes))
    if keep <= 1:
        return [indexes - 1]
    return sorted({round(i * (indexes - 1) / (keep - 1)) for i in range(keep)})


def _cartesian(
    block: ChartBlock, half: bool, shrink: float = 1.0
) -> tuple[list[str], list[_Series]]:
    """x + series：柱状类按 sort 排序并截断，折线类均匀降采样；null 原样保留成断点。"""
    x = list(block.x)
    names = _unique((s.name for s in block.series), "系列")
    series = [
        _Series(name, list(s.values), s.color, s.kind, s.axis)
        for name, s in zip(names, block.series, strict=True)
    ]
    if block.chart in {"line", "area"}:
        limit = HALF_LINE_MAX_POINTS if half else LINE_MAX_POINTS
        keep = _sample(len(x), max(2, int(limit * shrink)))
    elif block.chart == "radar":
        keep = list(range(min(len(x), max(3, int(RADAR_MAX_DIMS * shrink)))))
    else:
        order = list(range(len(x)))
        if block.sort in {"desc", "asc"}:
            totals = [sum(s.values[i] or 0.0 for s in series) for i in order]
            order.sort(key=lambda i: totals[i], reverse=block.sort == "desc")
        limit = max(1, int((HALF_BAR_MAX_CATEGORIES if half else BAR_MAX_CATEGORIES) * shrink))
        # 排过序时保留前面（排名靠前）；没排序多半是时间序列，保留最近的一段。
        keep = order[:limit] if block.sort in {"desc", "asc"} else order[-limit:]
    return [x[i] for i in keep], [
        _Series(s.name, [s.values[i] for i in keep], s.color, s.kind, s.axis) for s in series
    ]


def _values(series: Iterable[_Series]) -> list[float]:
    return [v for s in series for v in s.values if v is not None]


def _items(block: ChartBlock, shrink: float = 1.0) -> list[_Item]:
    """items：按 sort 排序，超过 top_n 的合并成「其他」（放最后）；漏斗和进度只截断。"""
    names = _unique((i.name for i in block.items), "项")
    items = [_Item(n, i.value, i.color) for n, i in zip(names, block.items, strict=True)]
    default_sort = "desc" if block.chart in {"hbar", "funnel"} else "none"
    order = block.sort or default_sort
    if block.chart in {"funnel", "progress"}:
        limit = block.top_n or (FUNNEL_MAX if block.chart == "funnel" else PROGRESS_MAX)
        items = _sorted(items, order, block.other_name)[: max(1, int(limit * shrink))]
        return items
    if block.chart == "ring":
        return items[:1]
    top_n = max(
        1, int((block.top_n or (HBAR_TOP_N if block.chart == "hbar" else PIE_TOP_N)) * shrink)
    )
    rest = [i for i in items if i.name != block.other_name]
    other = next((i for i in items if i.name == block.other_name), None)
    if len(rest) > top_n:
        ranked = sorted(range(len(rest)), key=lambda k: rest[k].value, reverse=True)
        kept = set(ranked[:top_n])
        merged = sum(rest[k].value for k in ranked[top_n:]) + (other.value if other else 0.0)
        color = other.color if other else None
        other = _Item(block.other_name, merged, color)
        rest = [rest[k] for k in sorted(kept)]
    items = _sorted(rest, order, block.other_name)
    return [*items, other] if other else items


def _sorted(items: list[_Item], order: str, other_name: str) -> list[_Item]:
    """按值排序，「其他」始终排最后。"""
    rest = [i for i in items if i.name != other_name]
    tail = [i for i in items if i.name == other_name]
    if order in {"desc", "asc"}:
        rest.sort(key=lambda i: i.value, reverse=order == "desc")
    return rest + tail


def _marked(
    values: Sequence[float | None], mode: str | list[str] | None, x: Sequence[str]
) -> set[int]:
    """labels / highlight 选中的下标：last 是最后一个非空值，max 是最大值，数组按类目名匹配。"""
    present = [i for i, v in enumerate(values) if v is not None]
    if not present or mode is None:
        return set()
    if mode == "last":
        return {present[-1]}
    if mode == "max":
        return {max(present, key=lambda i: values[i] or 0.0)}
    if isinstance(mode, list):
        wanted = set(mode)
        return {i for i in present if x[i] in wanted}
    return set(present) if mode == "all" else set()


# ---------------------------------------------------------------- 公共片段


def _base(kind: str, padding: dict[str, int]) -> dict[str, Any]:
    return {
        "type": kind,
        "background": "transparent",
        "padding": dict(padding),
        "hover": {"enable": False},
        "select": {"enable": False},
    }


def _theme(*series_types: str, secondary: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    """数据标签颜色写进 theme：数值标签用主文字色，次要标签用轴文字色，深浅主题都清楚。"""
    series: dict[str, Any] = {
        t: {"label": {"style": {"fill": dict(style.LABEL_PRIMARY)}}} for t in series_types
    }
    for series_type, mark in secondary:
        series.setdefault(series_type, {})[mark] = {"style": {"fill": dict(style.LABEL_SECONDARY)}}
    return {"series": series}


def _label(
    visible: bool, position: str | None, formatter: str = "{t}", **extra: Any
) -> dict[str, Any]:
    """数据标签：文本取预先算好的字段；lineWidth 0 去掉默认的背景色描边（深色下是重影）。"""
    label: dict[str, Any] = {"visible": visible, "formatter": formatter}
    if position:
        label["position"] = position
    label["style"] = {"lineWidth": 0, **extra}
    return label


def _legend(block: ChartBlock, multi: bool) -> dict[str, Any]:
    """auto：单系列隐藏、多系列放底部。"""
    visible = block.legend == "bottom" or (block.legend == "auto" and multi)
    return {"visible": True, "orient": "bottom"} if visible else {"visible": False}


def _band_axis(orient: str = "bottom") -> dict[str, Any]:
    return {"orient": orient, "type": "band", "label": {"autoHide": True, "autoLimit": True}}


def _value_axis(formatter: str, *, zero: bool = True, orient: str = "left") -> dict[str, Any]:
    axis: dict[str, Any] = {
        "orient": orient,
        "type": "linear",
        "tick": {"tickCount": 4},
        "label": {"formatter": formatter},
    }
    if not zero:
        axis["zero"] = False
    return axis


def _nice_max(value: float) -> float:
    """雷达半径的整齐上限：略大于最大值的 1/2/2.5/5 × 10^n。"""
    if value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if step * magnitude >= value * 1.05:
            return _compact(step * magnitude)
    return _compact(10 * magnitude)


def _share_text(value: float, total: float, fmt: _Format, percent: bool) -> str:
    return _percent_text(value / total) if percent and total else fmt.text(value)


# ---------------------------------------------------------------- 折线、面积（T1 / T2）


def _line(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """common 组合：主序列 line/area；末点、最大值、高亮点用一个 scatter 序列标出。"""
    x, series = _cartesian(block, half, shrink)
    fmt = _format(block.format, _values(series))
    kind = "area" if block.chart == "area" else "line"
    multi = len(series) > 1
    stacked = kind == "area" and multi and block.stack != "none"
    percent = stacked and block.stack == "percent"
    colors = _palette_colors(block, [s.color for s in series])
    label_all = block.labels == "all"

    rows: list[dict[str, Any]] = []
    for i, xv in enumerate(x):
        total = sum(abs(s.values[i] or 0.0) for s in series)
        for s in series:
            v = s.values[i]
            row: dict[str, Any] = {"x": xv, "y": None if v is None else fmt.value(v), "s": s.name}
            if label_all and v is not None:
                row["t"] = _share_text(v, total, fmt, percent)
            rows.append(row)

    # 堆叠时 scatter 不跟着堆叠，点会画错位置，所以不标点。
    highlighted = bool(block.highlight) and not stacked
    mode: str | list[str] | None = None
    if highlighted:
        mode = block.highlight
    elif not stacked and block.labels in {"auto", "last", "max"}:
        mode = "max" if block.labels == "max" else "last"
    show_text = block.labels != "none"
    marks: list[dict[str, Any]] = []
    for s in series:
        for i in sorted(_marked(s.values, mode, x)):
            v = s.values[i]
            if v is not None:
                text = fmt.text(v) if show_text else ""
                marks.append({"x": x[i], "y": fmt.value(v), "s": s.name, "t": text})

    line_style: dict[str, Any] = {"lineWidth": 2}
    if block.smooth:
        line_style["curveType"] = "monotone"
    main: dict[str, Any] = {
        "type": kind,
        "id": "main",
        "dataIndex": 0,
        "xField": "x",
        "yField": "y",
        "seriesField": "s",
        "line": {"style": line_style},
        "point": {"visible": len(x) <= LINE_POINT_MAX, "style": {"size": 6, "lineWidth": 0}},
        "label": _label(label_all, "top", fontSize=11),
    }
    if stacked:
        main["stack"] = True
        if percent:
            main["percent"] = True
    if kind == "area" and not multi:
        # 单系列用竖向渐变（真机验证过的写法）；多系列交给主题的默认透明度，避免互相遮挡。
        main["area"] = {
            "style": {
                "fill": {
                    "gradient": "linear",
                    "x0": 0,
                    "y0": 0,
                    "x1": 0,
                    "y1": 1,
                    "stops": [
                        {"offset": 0, "color": _rgba(colors[0], 0.32)},
                        {"offset": 1, "color": _rgba(colors[0], 0.02)},
                    ],
                }
            }
        }

    spec = _base("common", HALF_PADDING if half else PADDING)
    data: list[dict[str, Any]] = [{"id": "main", "values": rows}]
    chart_series: list[dict[str, Any]] = [main]
    if marks:
        spec["padding"]["top"] += 6
        data.append({"id": "mark", "values": marks})
        point: dict[str, Any] = {"size": 9 if highlighted else 7, "lineWidth": 0}
        label = _label(show_text, "top", fontSize=12, fontWeight="bold")
        if highlighted:
            # 刻意强调：点和标签都用高亮橙，不跟主题走。
            point["fill"] = style.CHART_HIGHLIGHT
            label["style"]["fill"] = style.CHART_HIGHLIGHT
        chart_series.append(
            {
                "type": "scatter",
                "id": "mark",
                "dataIndex": 1,
                "xField": "x",
                "yField": "y",
                "seriesField": "s",
                "point": {"style": point},
                "label": label,
            }
        )
    zero = True if block.y_zero is None else block.y_zero
    y_axis = _value_axis(
        "{label:.0%}" if percent else fmt.axis(_values(series)), zero=zero or stacked
    )
    spec.update(
        {
            "data": data,
            "color": colors,
            "series": chart_series,
            "axes": [_band_axis(), y_axis],
            "legends": _legend(block, multi),
            "theme": _theme(kind, "scatter"),
        }
    )
    return spec


# ---------------------------------------------------------------- 柱状（T3a / T3b）


def _bar(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """分组、堆叠、百分比堆叠；单系列可按类目高亮（按类目分色，真机验证过的写法）。"""
    x, series = _cartesian(block, half, shrink)
    fmt = _format(block.format, _values(series))
    multi = len(series) > 1
    stacked = multi and block.stack != "none"
    percent = stacked and block.stack == "percent"
    colors = _palette_colors(block, [s.color for s in series])
    bars = len(x) * (1 if stacked else len(series))

    mode: str | None
    if block.labels == "auto":
        mode = "all" if bars <= BAR_LABEL_MAX and not stacked else None
    else:
        mode = None if block.labels == "none" else block.labels
    labelled = [_marked(s.values, mode, x) for s in series]
    with_text = any(labelled)
    lit = _marked(series[0].values, block.highlight, x) if block.highlight and not multi else set()

    rows: list[dict[str, Any]] = []
    for i, xv in enumerate(x):
        total = sum(abs(s.values[i] or 0.0) for s in series)
        for k, s in enumerate(series):
            v = s.values[i]
            row: dict[str, Any] = {"x": xv, "y": None if v is None else fmt.value(v)}
            if multi:
                row["s"] = s.name
            if with_text:
                shown = v is not None and i in labelled[k]
                row["t"] = _share_text(v, total, fmt, percent) if v is not None and shown else ""
            rows.append(row)

    spec = _base("bar", HALF_PADDING if half else PADDING)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "xField": ["x", "s"] if multi and not stacked else "x",
            "yField": "y",
            "barMaxWidth": 24 if multi else 28,
        }
    )
    if multi:
        spec.update({"seriesField": "s", "color": colors})
    elif lit:
        spec.update(
            {
                "seriesField": "x",
                "color": [style.CHART_HIGHLIGHT if i in lit else colors[0] for i in range(len(x))],
            }
        )
    else:
        spec["color"] = colors[:1]
    if stacked:
        spec["stack"] = True
        if percent:
            spec["percent"] = True
    else:
        spec["bar"] = {"style": {"cornerRadius": [3, 3, 0, 0]}}
        if multi:
            spec["barGapInGroup"] = 2
    spec["label"] = _label(with_text, "inside" if stacked else "top", fontSize=11)
    y_axis = _value_axis("{label:.0%}" if percent else fmt.axis(_values(series)))
    spec.update(
        {
            "axes": [_band_axis(), y_axis],
            "legends": _legend(block, multi),
            "theme": _theme("bar"),
        }
    )
    return spec


# ---------------------------------------------------------------- 排行条形（T4）


def _hbar(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """降序、第一行在最上面（水平条形的 Y 轴自动反向）；底轨只写圆角；轴 max 留 20% 给标签。"""
    items = _items(block, shrink)
    raws = [i.value for i in items]
    names = [i.name for i in items]
    fmt = _format(block.format, raws)
    colors = _ramp_colors(block, names, [i.color for i in items], distinct=False)
    lit = _marked(raws, block.highlight, names) if block.highlight else set()
    colors = [style.CHART_HIGHLIGHT if k in lit else c for k, c in enumerate(colors)]
    mode = "all" if block.labels == "auto" else (None if block.labels == "none" else block.labels)
    labelled = _marked(raws, mode, names)
    rows = [
        {
            "name": it.name,
            "value": fmt.value(it.value),
            "t": fmt.text(it.value) if k in labelled else "",
        }
        for k, it in enumerate(items)
    ]
    scaled = [fmt.value(v) for v in raws]
    peak = max([*scaled, fmt.value(block.max) if block.max else 0])
    low = min([*scaled, 0])
    radius = 5 if half else 6
    spec = _base("bar", ROW_PADDING_SPEC)
    spec.update(
        {
            "direction": "horizontal",
            "data": [{"id": "d", "values": rows}],
            "xField": "value",
            "yField": "name",
            "seriesField": "name",
            "color": colors,
            "barWidth": 10 if half else 12,
            "bar": {"style": {"cornerRadius": radius}},
            # 底轨不写颜色：主题默认是前景色 6% 透明，深浅自适应。
            "barBackground": {"visible": True, "style": {"cornerRadius": radius}},
            "label": _label(bool(labelled), "outside", fontSize=12),
            "axes": [
                {
                    "orient": "left",
                    "type": "band",
                    "domainLine": {"visible": False},
                    "tick": {"visible": False},
                    "label": {"autoLimit": True},
                },
                {
                    "orient": "bottom",
                    "type": "linear",
                    "visible": False,
                    "min": _compact(low * 1.2),
                    "max": _compact(peak * 1.2) or 1,
                },
            ],
            "legends": _legend(block, False),
            "theme": _theme("bar"),
        }
    )
    return spec


# ---------------------------------------------------------------- 饼、环（T5）


def _indicator(value: str, caption: str | None, *, size: int) -> dict[str, Any]:
    """中心文字：不写颜色，走主题的主、次文字色。"""
    indicator: dict[str, Any] = {
        "visible": True,
        "trigger": "none",
        "fixed": True,
        "limitRatio": 0.8,
        "title": {
            "visible": True,
            "style": {"text": value, "fontSize": size, "fontWeight": "bold"},
        },
    }
    if caption:
        indicator["content"] = [{"visible": True, "style": {"text": caption, "fontSize": 12}}]
    return indicator


def _pie(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """全宽开外部标签（名称 + 占比）不要图例；半宽只用图例。donut 中心显示 center 或合计。"""
    items = _items(block, shrink)
    raws = [i.value for i in items]
    names = [i.name for i in items]
    fmt = _format(block.format, raws)
    total = sum(max(v, 0.0) for v in raws)
    colors = _ramp_colors(block, names, [i.color for i in items], distinct=True)
    lit = _marked(raws, block.highlight, names) if block.highlight else set()
    colors = [style.CHART_HIGHLIGHT if k in lit else c for k, c in enumerate(colors)]
    if block.labels == "auto":
        mode: str | None = None if half else "all"
    else:
        mode = None if block.labels == "none" else block.labels
    labelled = _marked(raws, mode, names)
    rows = [
        {
            "name": it.name,
            "value": fmt.value(it.value),
            "t": f"{it.name} {_percent_text(it.value / total)}" if k in labelled and total else "",
        }
        for k, it in enumerate(items)
    ]
    donut = block.chart == "donut"
    outer = 0.7 if labelled else 0.82
    spec = _base("pie", HALF_PADDING if half else ROUND_PADDING)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "categoryField": "name",
            "valueField": "value",
            "color": colors,
            "outerRadius": outer,
            "padAngle": 1,
            "label": _label(bool(labelled), "outside"),
            "legends": _legend(block, not labelled),
            "theme": _theme("pie"),
        }
    )
    if donut:
        spec.update({"innerRadius": round(outer * 0.74, 2), "cornerRadius": 3})
        if block.center:
            value, caption = block.center.value, block.center.caption
        else:
            value, caption = fmt.text(total), "合计"
        spec["indicator"] = _indicator(value, caption, size=18 if half or labelled else 20)
    return spec


# ---------------------------------------------------------------- 漏斗（T6）


def _funnel(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """从宽到窄；层内标签「名称 数值」，层间转化率由 transformLabel 自动算。"""
    items = _items(block, shrink)
    raws = [i.value for i in items]
    names = [i.name for i in items]
    fmt = _format(block.format, raws)
    base = _single_color(block)
    lit = _marked(raws, block.highlight, names) if block.highlight else set()
    colors = [
        style.CHART_HIGHLIGHT if k in lit else (_color(it.color) or base)
        for k, it in enumerate(items)
    ]
    show = block.labels != "none"
    rows = [
        {"name": it.name, "value": fmt.value(it.value), "t": f"{it.name} {fmt.text(it.value)}"}
        for it in items
    ]
    spec = _base("funnel", HALF_PADDING if half else ROUND_PADDING)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "categoryField": "name",
            "valueField": "value",
            "color": colors,
            "isTransform": True,
            "label": _label(show, None, fontSize=11 if half else 12),
            "transformLabel": {"visible": show, "style": {"lineWidth": 0}},
            "outerLabel": {"visible": False, "style": {"lineWidth": 0}},
            "legends": _legend(block, False),
            "theme": _theme("funnel", secondary=[("funnel", "transformLabel")]),
        }
    )
    return spec


# ---------------------------------------------------------------- 组合图（T7）


def _combo(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """柱 + 线，双轴。kind 缺省时第一个系列是柱、其余是线；axis 缺省时柱在左、线在右。

    同 (kind, axis) 的系列合成一个 VChart 序列（柱子才能分组），各自一份数据。
    右轴用 format2，并与左轴刻度对齐。
    """
    x, series = _cartesian(block, half, shrink)
    kinds = [s.kind or ("bar" if k == 0 else "line") for k, s in enumerate(series)]
    sides = [
        s.axis or ("left" if kind == "bar" else "right")
        for s, kind in zip(series, kinds, strict=True)
    ]
    if "left" not in sides:
        sides = ["left"] * len(series)
    own_colors = _palette_colors(block, [s.color for s in series])
    left = [s for s, side in zip(series, sides, strict=True) if side == "left"]
    right = [s for s, side in zip(series, sides, strict=True) if side == "right"]
    fmts = {
        "left": _format(block.format, _values(left)),
        "right": _format(block.format2, _values(right)),
    }

    groups: dict[tuple[str, str], list[int]] = {}
    for k, key in enumerate(zip(kinds, sides, strict=True)):
        groups.setdefault(key, []).append(k)
    bar_count = sum(len(x) for k in range(len(series)) if kinds[k] == "bar")

    data: list[dict[str, Any]] = []
    chart_series: list[dict[str, Any]] = []
    colors: list[str] = []
    axis_ids: dict[str, list[str]] = {"left": [], "right": []}
    for index, ((kind, side), members) in enumerate(groups.items()):
        fmt = fmts[side]
        if block.labels == "auto":
            mode: str | None = (
                "last" if kind == "line" else ("all" if bar_count <= BAR_LABEL_MAX else None)
            )
        else:
            mode = None if block.labels == "none" else block.labels
        rows: list[dict[str, Any]] = []
        marked = {k: _marked(series[k].values, mode, x) for k in members}
        with_text = any(marked.values())
        for i, xv in enumerate(x):
            for k in members:
                v = series[k].values[i]
                row: dict[str, Any] = {"x": xv, "y": None if v is None else fmt.value(v)}
                row["s"] = series[k].name
                if with_text:
                    row["t"] = fmt.text(v) if v is not None and i in marked[k] else ""
                rows.append(row)
        sid = f"{kind}{index}"
        data.append({"id": sid, "values": rows})
        colors.extend(own_colors[k] for k in members)
        axis_ids[side].append(sid)
        item: dict[str, Any] = {
            "type": kind,
            "id": sid,
            "dataIndex": index,
            "xField": ["x", "s"] if kind == "bar" and len(members) > 1 else "x",
            "yField": "y",
            "seriesField": "s",
            "label": _label(with_text, "top", fontSize=11),
        }
        if kind == "bar":
            item.update({"barMaxWidth": 24, "bar": {"style": {"cornerRadius": [3, 3, 0, 0]}}})
        else:
            line_style: dict[str, Any] = {"lineWidth": 2}
            if block.smooth:
                line_style["curveType"] = "monotone"
            item.update(
                {
                    "line": {"style": line_style},
                    "point": {"visible": True, "style": {"size": 6, "lineWidth": 0}},
                }
            )
        chart_series.append(item)

    left_axis = _value_axis(fmts["left"].axis(_values(left)))
    left_axis.update({"id": "yL", "seriesId": axis_ids["left"]})
    axes = [_band_axis(), left_axis]
    if axis_ids["right"]:
        right_axis = _value_axis(fmts["right"].axis(_values(right)), orient="right")
        right_axis.update(
            {
                "id": "yR",
                "seriesId": axis_ids["right"],
                "grid": {"visible": False},
                "sync": {"axisId": "yL", "tickAlign": True},
            }
        )
        axes.append(right_axis)
    spec = _base("common", HALF_PADDING if half else PADDING)
    spec.update(
        {
            "data": data,
            "color": colors,
            "series": chart_series,
            "axes": axes,
            "legends": _legend(block, len(series) > 1),
            "theme": _theme("bar", "line"),
        }
    )
    return spec


# ---------------------------------------------------------------- 进度（T8a / T8b）


def _fractions(block: ChartBlock, items: Sequence[_Item]) -> list[float]:
    """完成度（0–1）：给了 max 按 max 算；percent_input=percent 或有值大于 1.5 时按百分数算。"""
    raws = [i.value for i in items]
    fmt = block.format
    if block.max:
        return [v / block.max for v in raws]
    percent = fmt is not None and fmt.percent_input == "percent"
    if percent or ((fmt is None or fmt.percent_input is None) and any(v > 1.5 for v in raws)):
        return [v / 100 for v in raws]
    return raws


def _decimals(block: ChartBlock) -> int | None:
    return block.format.decimals if block.format else None


def _progress(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """条形进度：linearProgress 没有数值标签，把百分比拼进类目名；底轨用半透明中性灰。"""
    items = _items(block, shrink)
    fractions = _fractions(block, items)
    names = [i.name for i in items]
    base = _single_color(block)
    lit = _marked(fractions, block.highlight, names) if block.highlight else set()
    rows: list[dict[str, Any]] = []
    colors: list[str] = []
    for k, (it, frac) in enumerate(zip(items, fractions, strict=True)):
        text = _percent_text(frac, _decimals(block))
        label = it.name if block.labels == "none" else f"{it.name} {text}"
        rows.append({"type": label, "value": _compact(min(max(frac, 0.0), 1.0))})
        colors.append(style.CHART_HIGHLIGHT if k in lit else (_color(it.color) or base))
    spec = _base("linearProgress", ROW_PADDING_SPEC)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "direction": "horizontal",
            "xField": "value",
            "yField": "type",
            "seriesField": "type",
            "color": colors,
            "bandWidth": 8 if half else 10,
            "cornerRadius": 5,
            "track": {"style": {"fill": TRACK_FILL}},
            "axes": [
                {
                    "orient": "left",
                    "type": "band",
                    "domainLine": {"visible": False},
                    "tick": {"visible": False},
                    "label": {"autoLimit": True},
                }
            ],
            "legends": {"visible": False},
            "theme": _theme("linearProgress"),
        }
    )
    return spec


def _ring(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """环形 KPI：只画第一项；中心是 center.value（缺省为百分比）和 center.caption（缺省为名称）。"""
    item = _items(block)[0]
    frac = _fractions(block, [item])[0]
    text = _percent_text(frac, _decimals(block))
    value = block.center.value if block.center else text
    caption = (block.center.caption if block.center else None) or item.name
    spec = _base("circularProgress", HALF_PADDING if half else ROUND_PADDING)
    spec.update(
        {
            "data": [
                {
                    "id": "d",
                    "values": [{"type": item.name, "value": _compact(min(max(frac, 0.0), 1.0))}],
                }
            ],
            "valueField": "value",
            "categoryField": "type",
            "seriesField": "type",
            "color": [_single_color(block, item.color)],
            "maxValue": 1,
            "outerRadius": 0.86,
            "innerRadius": 0.7,
            "cornerRadius": 20,
            # track 默认是系列色的 20% 透明度，本身深浅自适应，不写。
            "indicator": _indicator(value, caption, size=20 if half else 22),
            "legends": {"visible": False},
            "theme": _theme("circularProgress"),
        }
    )
    return spec


# ---------------------------------------------------------------- 雷达（T9）


def _radar(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    x, series = _cartesian(block, half, shrink)
    fmt = _format(block.format, _values(series))
    multi = len(series) > 1
    label_all = block.labels == "all"
    rows: list[dict[str, Any]] = []
    for s in series:
        for i, dim in enumerate(x):
            v = s.values[i]
            row: dict[str, Any] = {"k": dim, "v": None if v is None else fmt.value(v), "s": s.name}
            if label_all and v is not None:
                row["t"] = fmt.text(v)
            rows.append(row)
    peak = max((abs(fmt.value(v)) for v in _values(series)), default=0.0)
    top = fmt.value(block.max) if block.max else _nice_max(peak)
    spec = _base("radar", HALF_PADDING if half else ROUND_PADDING)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "categoryField": "k",
            "valueField": "v",
            "seriesField": "s",
            "color": _palette_colors(block, [s.color for s in series]),
            "outerRadius": 0.68 if half else 0.72,
            "area": {"visible": True, "style": {"fillOpacity": 0.15}},
            "line": {"style": {"lineWidth": 2}},
            "point": {"visible": False, "style": {"lineWidth": 0}},
            "label": _label(label_all, None, fontSize=11),
            "axes": [
                {"orient": "radius", "min": 0, "max": top, "label": {"visible": False}},
                {"orient": "angle", "label": {"space": 4}},
            ],
            "legends": _legend(block, multi),
            "theme": _theme("radar"),
        }
    )
    return spec


# ---------------------------------------------------------------- 散点（T10）


def _scatter(block: ChartBlock, half: bool, shrink: float = 1.0) -> dict[str, Any]:
    """group 分色，size 画成气泡；点太多均匀抽样。y 轴默认不从 0 开始。"""
    keep = max(1, int(SCATTER_MAX_POINTS * shrink))
    points = [block.points[i] for i in _sample(len(block.points), keep)]
    ys = [p.y for p in points]
    fmt = _format(block.format, ys)
    grouped = any(p.group for p in points)
    sized = any(p.size is not None for p in points)
    raw_groups = list(dict.fromkeys(p.group or "" for p in points)) if grouped else []
    groups = _unique(raw_groups, "组")
    rename = dict(zip(raw_groups, groups, strict=True))
    label_all = block.labels == "all"
    rows: list[dict[str, Any]] = []
    for p in points:
        row: dict[str, Any] = {"x": _compact(p.x), "y": fmt.value(p.y)}
        if grouped:
            row["s"] = rename[p.group or ""]
        if sized:
            row["z"] = _compact(p.size or 0.0)
        if label_all:
            row["t"] = fmt.text(p.y)
        rows.append(row)
    point: dict[str, Any] = {"fillOpacity": 0.75, "lineWidth": 0}
    if not sized:
        point["size"] = 6 if half else 8
    spec = _base("scatter", HALF_PADDING if half else PADDING)
    spec.update(
        {
            "data": [{"id": "d", "values": rows}],
            "xField": "x",
            "yField": "y",
            "color": _palette_colors(block, [None] * max(len(groups), 1)),
            "point": {"style": point},
            "label": _label(label_all, "top", fontSize=11),
        }
    )
    if grouped:
        spec["seriesField"] = "s"
    if sized:
        spec.update({"sizeField": "z", "size": [6, 20 if half else 24]})
    x_places = _axis_decimals(max((abs(p.x) for p in points), default=0.0), None)
    y_axis = _value_axis(fmt.axis(ys), zero=bool(block.y_zero))
    spec.update(
        {
            "axes": [_value_axis(f"{{label:,.{x_places}f}}", zero=False, orient="bottom"), y_axis],
            "legends": _legend(block, len(groups) > 1),
            "theme": _theme("scatter"),
        }
    )
    return spec


# ---------------------------------------------------------------- 对外接口

_BUILDERS = {
    "line": _line,
    "area": _line,
    "bar": _bar,
    "hbar": _hbar,
    "pie": _pie,
    "donut": _pie,
    "funnel": _funnel,
    "combo": _combo,
    "progress": _progress,
    "ring": _ring,
    "radar": _radar,
    "scatter": _scatter,
}
# 小环、进度点开全屏没有意义，关掉预览，免得移动端一点就进全屏。
_NO_PREVIEW = frozenset({"ring", "progress"})


def spec_size(spec: dict[str, Any]) -> int:
    """spec 体积（字节）：与投递时的 json.dumps(ensure_ascii=False) 口径一致。"""
    return len(json.dumps(spec, ensure_ascii=False).encode())


def chart_spec(block: ChartBlock, *, half: bool = False) -> dict[str, Any]:
    """DSL → VChart spec（纯函数，便于测试）。half 为半宽分栏：数据量、内边距、标签都收紧。

    超出 SPEC_BUDGET 时逐级减少数据点（降采样、截断、缩小 top_n）重算，保证单个图表不撑爆卡片。
    """
    build = _BUILDERS[block.chart]
    spec = build(block, half)
    for shrink in _SHRINK[1:]:
        if spec_size(spec) <= SPEC_BUDGET:
            break
        spec = build(block, half, shrink)
    return spec


def chart_height(block: ChartBlock, spec: dict[str, Any], *, half: bool = False) -> int:
    """固定高度（px）：block.height 优先；排行条形、条形进度按实际行数 32×N+24（上限 360）；
    半宽 180；饼、环、雷达 220；其余 240。"""
    if block.height:
        return style.CHART_HEIGHT[block.height]
    if block.chart in {"hbar", "progress"}:
        rows = len(spec["data"][0]["values"])
        return min(ROW_HEIGHT * rows + ROW_PADDING, MAX_ROW_CHART_HEIGHT)
    if half:
        return style.CHART_HALF_HEIGHT
    if block.chart in {"pie", "donut", "ring", "radar"}:
        return ROUND_HEIGHT
    return TREND_HEIGHT


def _markdown(ctx: RenderContext, content: str, size: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "element_id": ctx.new_id("md"),
        "content": content,
        "text_size": size,
    }


def render_chart(
    block: ChartBlock, ctx: RenderContext, *, half: bool = False
) -> list[dict[str, Any]]:
    """[标题 markdown?, chart 组件, 结论 markdown?]。

    图表名额用完（ctx.take_chart() 为 False）时返回 []，由调用方降级成数据表。
    """
    if not ctx.take_chart():
        return []
    out: list[dict[str, Any]] = []
    if block.title:
        out.append(_markdown(ctx, f"**{block.title}**", style.TEXT_BODY))
    spec = chart_spec(block, half=half)
    out.append(
        {
            "tag": "chart",
            "element_id": ctx.new_id("chart"),
            "height": f"{chart_height(block, spec, half=half)}px",
            "chart_spec": spec,
            "preview": block.chart not in _NO_PREVIEW,
        }
    )
    if block.summary:
        out.append(_markdown(ctx, f"<font color='grey'>{block.summary}</font>", style.TEXT_NOTE))
    return out
