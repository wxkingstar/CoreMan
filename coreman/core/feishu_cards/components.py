"""富内容块 → 飞书卡片 JSON 2.0 组件。

每个渲染函数只负责把一个块变成一组组件，不关心它在卡片里的位置；配色、字号、圆角全部取自
`style`，这里不写任何具体色值，文字颜色只用 `<font color='色相名'>`。组件都带 element_id，
由 `RenderContext` 统一分配，保证全卡唯一。

结构约定（都在真机的深浅两种主题、PC 与手机上看过）：
- 圆角底色块只能用 `interactive_container`（`behaviors: []`），分栏和列没有圆角；
- 容器嵌套最深的是指标块放进并排两栏：column_set → column → column_set → column →
  interactive_container，正好 5 层，不能再往里套容器。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from coreman.core.feishu_cards import reply_buttons, sanitize, style
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.markdown import normalize
from coreman.core.richtext.schema import (
    ActionsBlock,
    CalloutBlock,
    HeaderBlock,
    ItemBlock,
    KpiBlock,
    KpiItem,
    NoteBlock,
    PeopleBlock,
    RawBlock,
    TableBlock,
    TableColumn,
    Tag,
    TimelineBlock,
)

Element = dict[str, Any]

# 按钮样式：主操作蓝底白字，危险操作红字描边，其余默认描边。
BUTTON_TYPES: dict[str, str] = {
    "primary": "primary_filled",
    "danger": "danger",
    "default": "default",
}
# 表格表头：浅灰底、灰字、加粗，与 markdown 表格的观感一致。
TABLE_HEADER_STYLE: dict[str, Any] = {
    "background_style": "grey",
    "text_color": "grey",
    "bold": True,
    "text_size": "normal",
}
# 表格 tag 列没写颜色时，常见状态词按语义上色，其余按顺序轮换。
TAG_SEMANTIC: dict[str, str] = {
    **dict.fromkeys(
        ("完成", "已完成", "成功", "正常", "通过", "达标", "已发货", "已签收", "done", "ok"),
        "green",
    ),
    **dict.fromkeys(
        ("失败", "异常", "错误", "已取消", "超时", "拒绝", "error", "failed"),
        "red",
    ),
    **dict.fromkeys(
        ("警告", "待处理", "延迟", "偏低", "偏高", "缺货", "预警", "warning", "pending"),
        "orange",
    ),
    **dict.fromkeys(("进行中", "处理中", "运输中", "running"), "blue"),
    **dict.fromkeys(("未开始", "草稿", "暂停", "draft"), "neutral"),
}
TAG_ROTATION: tuple[str, ...] = tuple(
    c
    for c in ("blue", "turquoise", "violet", "wathet", "indigo", "lime", "purple", "carmine")
    if c in style.TAG_COLORS
)
# 时间线左栏宽度：放得下「09/20 14:30」加图标。
TIMELINE_TIME_WIDTH = "96px"
ITEM_IMAGE_SIZE = "72px 72px"
KPI_MAX_PER_ROW = 4

# markdown 里认的行内标签；其余的 `<` 转成实体，免得被当成 HTML 吞掉。
_INLINE_TAGS = re.compile(
    r"<(?!/?(?:font|text_tag|a|link|at|person|local_datetime|br|hr)\b)", re.IGNORECASE
)
_SIGN_DOWN = ("-", "−", "–", "↓")
_SIGN_UP = ("+", "↑")


# ---------------------------------------------------------------- 基础件


def _inline(text: str) -> str:
    """块里的短文字放进 markdown：保留飞书行内标签，其他 `<` 转义，换行并成空格。"""
    return _INLINE_TAGS.sub("&#60;", text).replace("\r", "").replace("\n", " ")


def _font(text: str, color: str | None) -> str:
    return f"<font color='{color}'>{text}</font>" if color else text


def _md(
    ctx: RenderContext,
    content: str,
    *,
    size: str | None = None,
    icon: dict[str, Any] | None = None,
) -> Element:
    node: Element = {"tag": "markdown", "element_id": ctx.new_id("md"), "content": content}
    if size:
        node["text_size"] = size
    if icon:
        node["icon"] = icon
    return node


def _column(
    ctx: RenderContext,
    elements: list[Element],
    *,
    width: str = "weighted",
    weight: int = 1,
    **extra: Any,
) -> Element:
    node: Element = {"tag": "column", "element_id": ctx.new_id("col"), "width": width}
    if width == "weighted":
        node["weight"] = max(1, min(5, weight))
    node.update(extra)
    node["elements"] = elements
    return node


def _column_set(
    ctx: RenderContext,
    columns: list[Element],
    *,
    flex_mode: str,
    spacing: str,
    element_id: str | None = None,
) -> Element:
    return {
        "tag": "column_set",
        "element_id": element_id or ctx.new_id("cols"),
        "flex_mode": flex_mode,
        "horizontal_spacing": spacing,
        "columns": columns,
    }


def _caption(ctx: RenderContext, title: str) -> Element:
    """块上方的小标题（表格、人员等）。"""
    return _md(ctx, f"**{_inline(title)}**", size=style.TEXT_BODY)


def panel(
    elements: list[dict[str, Any]],
    ctx: RenderContext,
    *,
    bg: str = style.PANEL,
    border: str | None = None,
    padding: str = style.PANEL_PADDING,
    spacing: str = "4px",
) -> dict[str, Any]:
    """圆角面板：interactive_container 不带交互（`behaviors: []`，真机验证可用）。"""
    node: Element = {
        "tag": "interactive_container",
        "element_id": ctx.new_id("panel"),
        "behaviors": [],
        "background_style": bg,
        "corner_radius": style.RADIUS,
        "padding": padding,
        "vertical_spacing": spacing,
    }
    if border:
        node["has_border"] = True
        node["border_color"] = border
    node["elements"] = elements
    return node


def two_columns(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    ctx: RenderContext,
    *,
    ratio: tuple[int, int] = (1, 1),
) -> dict[str, Any]:
    """并排两栏：宽屏左右排，窄屏上下堆叠（flex_mode stretch），各自占满宽度。"""
    return _column_set(
        ctx,
        [
            _column(ctx, left, weight=ratio[0], vertical_align="top"),
            _column(ctx, right, weight=ratio[1], vertical_align="top"),
        ],
        flex_mode="stretch",
        spacing="12px",
    )


def columns(cols: list[list[dict[str, Any]]], ctx: RenderContext) -> dict[str, Any]:
    """2–3 栏等宽并排，窄屏上下堆叠。"""
    if not 2 <= len(cols) <= 3:
        raise ValueError(f"columns 需要 2–3 栏，收到 {len(cols)} 栏")
    return _column_set(
        ctx,
        [_column(ctx, col, vertical_align="top") for col in cols],
        flex_mode="stretch",
        spacing="12px",
    )


# ---------------------------------------------------------------- 标题栏


def render_header(block: HeaderBlock) -> dict[str, Any]:
    """卡片的 header 字段（不是 body 里的组件）。"""
    header: dict[str, Any] = {"title": {"tag": "plain_text", "content": block.title}}
    if block.subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": block.subtitle}
    if block.tags:
        header["text_tag_list"] = [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag.text},
                "color": tag.color if tag.color in style.TAG_COLORS else "neutral",
            }
            for tag in block.tags[:3]
        ]
    if block.color and block.color in style.HEADER_TEMPLATES:
        header["template"] = block.color
    icon = style.icon(block.icon)
    if icon:
        header["icon"] = icon
    return header


# ---------------------------------------------------------------- 指标块


def _delta(item: KpiItem) -> str | None:
    """变化值：按好坏着色，按正负号加箭头。"""
    if not item.delta:
        return None
    text = item.delta.strip()
    arrow = ""
    if text.startswith(_SIGN_DOWN):
        arrow, text = "↓", text[1:].lstrip()
    elif text.startswith(_SIGN_UP):
        arrow, text = "↑", text[1:].lstrip()
    color = style.NEUTRAL if item.good is None else (style.GOOD if item.good else style.BAD)
    return _font(f"{arrow}{_inline(text)}", color)


def _kpi_tile(item: KpiItem, ctx: RenderContext) -> Element:
    elements = [
        _md(ctx, f"**{_inline(item.value)}**", size=style.TEXT_KPI),
        _md(ctx, _font(_inline(item.label), "grey"), size=style.TEXT_NOTE),
    ]
    note = _font(_inline(item.note), "grey") if item.note else None
    line = "  ".join(p for p in (_delta(item), note) if p)
    if line:
        elements.append(_md(ctx, line, size=style.TEXT_NOTE))
    return panel(elements, ctx, padding=style.TILE_PADDING, spacing="2px")


def render_kpi(block: KpiBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """指标块。

    整行：每行 N 个圆角小块（N 取 columns，否则 ≤4 个一行、5–8 个两行），flex_mode bisect，
    手机上自动变成 2×2。半宽（size=half）：只给纵向排列的小块，由调用方放进并排两栏。
    """
    tiles = [_kpi_tile(item, ctx) for item in block.items]
    if block.size == "half":
        return tiles
    count = len(tiles)
    per_row = block.columns or (count if count <= KPI_MAX_PER_ROW else math.ceil(count / 2))
    rows: list[Element] = []
    for start in range(0, count, per_row):
        chunk = tiles[start : start + per_row]
        cols = [_column(ctx, [tile]) for tile in chunk]
        # 末行不足时补空列，让各行的块等宽对齐。
        cols += [_column(ctx, []) for _ in range(per_row - len(chunk)) if count > per_row]
        rows.append(_column_set(ctx, cols, flex_mode="bisect", spacing="8px"))
    return rows


# ---------------------------------------------------------------- 提示条与脚注


def render_callout(block: CalloutBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """提示条：语义色浅底加同色系边框，标题行带状态图标。"""
    level = style.LEVELS[block.level]
    icon = style.icon(level["icon"], level["text"])
    elements: list[Element] = []
    if block.title:
        elements.append(_md(ctx, f"**{_inline(block.title)}**", icon=icon))
    if block.text:
        # 正文是模型写的 Markdown，和普通正文一样要先规范化（缩进、代码语言、HTML、链接）。
        body = normalize(block.text, ctx.images)
        elements.append(_md(ctx, body, icon=None if block.title else icon))
    return [panel(elements, ctx, bg=level["bg"], border=level["border"], spacing="6px")]


def render_note(block: NoteBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """口径或脚注：信息图标加灰色小字。"""
    node: Element = {"tag": "div", "element_id": ctx.new_id("note")}
    icon = style.icon("info_outlined", "grey")
    if icon:
        node["icon"] = icon
    node["text"] = {
        "tag": "plain_text",
        "content": block.text,
        "text_size": style.TEXT_NOTE,
        "text_color": "grey",
    }
    return [node]


# ---------------------------------------------------------------- 实体卡


def _tag_markup(tags: Iterable[Tag]) -> str:
    return " ".join(
        f"<text_tag color='{t.color if t.color in style.TAG_COLORS else 'neutral'}'>"
        f"{_inline(t.text)}</text_tag>"
        for t in tags
    )


def render_item(block: ItemBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """左图右文的实体卡（商品、订单、人）；有链接时整块可点。"""
    text: list[Element] = []
    if block.eyebrow:
        text.append(_md(ctx, _font(_inline(block.eyebrow), "grey"), size=style.TEXT_NOTE))
    text.append(_md(ctx, f"**{_inline(block.title)}**", size=style.TEXT_TITLE))
    parts: list[str] = []
    if block.meta:
        meta = " · ".join(_inline(m) for m in block.meta if m)
        parts.append(_font(meta + (" · " if block.highlight else ""), "grey"))
    if block.highlight:
        color = style.LEVELS[block.highlight.level]["text"]
        parts.append(_font(_inline(block.highlight.text), color))
    if parts:
        text.append(_md(ctx, "".join(parts), size=style.TEXT_NOTE))
    if block.code:
        code = block.code.replace("`", "")
        if code:
            text.append(_md(ctx, f"`{code}`", size=style.TEXT_NOTE))
    if block.tags:
        text.append(_md(ctx, _tag_markup(block.tags), size=style.TEXT_NOTE))

    key = ctx.image_key(block.image) if block.image else None
    if key:
        image: Element = {
            "tag": "img",
            "element_id": ctx.new_id("img"),
            "img_key": key,
            "alt": {"tag": "plain_text", "content": block.title},
            "scale_type": "crop_center",
            "size": ITEM_IMAGE_SIZE,
            "corner_radius": style.RADIUS,
            "preview": True,
        }
        body: list[Element] = [
            _column_set(
                ctx,
                [
                    _column(ctx, [image], width="auto", vertical_align="center"),
                    _column(ctx, text, vertical_align="center", vertical_spacing="2px"),
                ],
                flex_mode="none",
                spacing="12px",
            )
        ]
        node = panel(body, ctx)
    else:
        node = panel(text, ctx, spacing="2px")
    if block.link and sanitize.is_http_url(block.link):
        node["behaviors"] = [{"type": "open_url", "default_url": block.link.strip()}]
    return [node]


# ---------------------------------------------------------------- 时间线


def render_timeline(block: TimelineBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """时间线：每步一行，左栏状态图标加时间，右栏说明；未完成的步骤用状态色。"""
    has_time = any(step.time for step in block.steps)
    rows: list[Element] = []
    for step in block.steps:
        token, color = style.STATUS[step.status]
        icon = style.icon(token, color)
        tint = None if step.status == "done" else color
        text = _font(_inline(step.text), tint)
        if not has_time:
            rows.append(_md(ctx, text, icon=icon))
            continue
        when = _font(_inline(step.time), tint) if step.time else "&nbsp;"
        rows.append(
            _column_set(
                ctx,
                [
                    _column(
                        ctx,
                        [_md(ctx, when, icon=icon)],
                        width=TIMELINE_TIME_WIDTH,
                        vertical_align="top",
                    ),
                    _column(ctx, [_md(ctx, text)], vertical_align="top"),
                ],
                flex_mode="none",
                spacing="12px",
            )
        )
    return rows


# ---------------------------------------------------------------- 人员


def render_people(block: PeopleBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """人员展示（不发通知）：解析到 ID 的用头像列表，解析不了的按名字灰字列出。"""
    ids: list[str] = []
    names: list[str] = []
    for user in block.users:
        uid = ctx.people.get(user)
        if uid:
            if uid not in ids:
                ids.append(uid)
        elif user not in names:
            names.append(user)
    out: list[Element] = []
    if block.title:
        out.append(_caption(ctx, block.title))
    if ids:
        out.append(
            {
                "tag": "person_list",
                "element_id": ctx.new_id("people"),
                "persons": [{"id": uid} for uid in ids],
                "drop_invalid_user_id": True,
                "show_avatar": True,
                "show_name": True,
                "size": "small",
            }
        )
    if names:
        out.append(_md(ctx, _font(_inline("、".join(names)), "grey"), size=style.TEXT_BODY))
    return out


# ---------------------------------------------------------------- 按钮


def render_actions(block: ActionsBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """一排按钮：链接按钮直接跳转；回复按钮走回调，把按钮上的字当作提问人的下一句话。

    回复按钮只在提问人自己的对话回复里可点（`ctx.requester`），value 带上这一行和行内回复按钮
    的 element_id，点过之后据此置灰。其余出口（定时任务结果、推给别人的消息）回复按钮只显示
    成灰字，不带任何点击行为。
    """
    row = ctx.new_id("cols")
    requester = ctx.requester
    links = [b for b in block.buttons if b.url and sanitize.is_http_url(b.url)]
    replies = [b for b in block.buttons if b.is_reply]
    kept = [b for b in block.buttons if b in links or (requester and b in replies)]
    ids = [ctx.new_id("btn") for _ in kept]
    reply_ids = [bid for bid, b in zip(ids, kept, strict=True) if b.is_reply]
    cols: list[Element] = []
    for bid, button in zip(ids, kept, strict=True):
        node: Element = {
            "tag": "button",
            "element_id": bid,
            "type": BUTTON_TYPES.get(button.style, "default"),
            "size": "small",
            "text": {"tag": "plain_text", "content": button.text},
        }
        if button.is_reply and requester:
            value = reply_buttons.reply_value(
                button.text, requester=requester, row=row, buttons=reply_ids
            )
            node["behaviors"] = [{"type": "callback", "value": value}]
        else:
            node["behaviors"] = [{"type": "open_url", "default_url": str(button.url).strip()}]
        cols.append(_column(ctx, [node], width="auto"))
    out: list[Element] = []
    if cols:
        out.append(_column_set(ctx, cols, flex_mode="flow", spacing="8px", element_id=row))
    if replies and not requester:
        hints = " · ".join(f"「{_inline(b.text)}」" for b in replies)
        out.append(_md(ctx, _font(hints, "grey"), size=style.TEXT_NOTE))
    return out


# ---------------------------------------------------------------- 表格


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and not math.isfinite(value) else float(value)
    if isinstance(value, str):
        text = re.sub(r"[,\s，]", "", value)
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return "、".join(_cell_text(v) for v in value)
    return str(value)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _person_ids(value: Any, people: Mapping[str, str]) -> list[str] | None:
    """单元格里的人（一个或多个）全都解析得到 ID 才返回。"""
    names = value if isinstance(value, list) else [value]
    found = [people.get(str(n)) for n in names]
    return None if any(f is None for f in found) else [str(f) for f in found]


def _tag_color(text: str, seen: dict[str, str]) -> str:
    semantic = TAG_SEMANTIC.get(text.strip().lower()) or TAG_SEMANTIC.get(text.strip())
    if semantic:
        return semantic
    if text not in seen:
        seen[text] = TAG_ROTATION[len(seen) % len(TAG_ROTATION)]
    return seen[text]


def _table_column(
    column: TableColumn, name: str, values: Sequence[Any], people: Mapping[str, str]
) -> tuple[Element, list[Any]]:
    """一列的定义与这一列每行的单元格值。数据不合列类型时退回文字列，不丢内容。"""
    spec: Element = {"name": name, "display_name": column.title or column.key}
    if column.align:
        spec["horizontal_align"] = column.align
    kind = column.type
    present = [v for v in values if not _blank(v)]
    cells: list[Any]

    if kind in {"number", "money"} and all(_number(v) is not None for v in present):
        symbol = (column.prefix or "$") if kind == "money" else None
        if symbol is None or len(symbol) == 1:
            fmt: dict[str, Any] = {"separator": True}
            if symbol:
                fmt["symbol"] = symbol
            precision = column.decimals
            if precision is None and kind == "money":
                precision = 2
            if precision is not None:
                fmt["precision"] = precision
            spec.update(data_type="number", format=fmt)
            cells = [None if _blank(v) else _as_number(_number(v) or 0.0) for v in values]
            return spec, cells
        # 多字符货币符号（如 US$）飞书不支持，按文字列格式化。
        decimals = column.decimals if column.decimals is not None else 2
        spec["data_type"] = "lark_md"
        spec.setdefault("horizontal_align", "right")
        cells = [
            "" if _blank(v) else f"{symbol}{(_number(v) or 0.0):,.{decimals}f}" for v in values
        ]
        return spec, cells

    if kind == "percent":
        numbers = [_number(v) for v in present if not (isinstance(v, str) and "%" in v)]
        ratio = bool(numbers) and all(n is not None and abs(n) <= 1 for n in numbers)
        decimals = column.decimals if column.decimals is not None else 1
        spec["data_type"] = "lark_md"
        spec.setdefault("horizontal_align", "right")
        cells = []
        for v in values:
            n = _number(str(v).replace("%", "")) if not _blank(v) else None
            if n is None:
                cells.append(_cell_text(v))
                continue
            if ratio and not (isinstance(v, str) and "%" in v):
                n *= 100
            cells.append(f"{n:.{decimals}f}%")
        return spec, cells

    if kind == "person":
        mapped = [_person_ids(v, people) for v in present]
        if present and all(m is not None for m in mapped):
            spec["data_type"] = "persons"
            return spec, [None if _blank(v) else _person_ids(v, people) for v in values]
        spec["data_type"] = "text"
        return spec, [_cell_text(v) for v in values]

    if kind == "tag":
        seen: dict[str, str] = {}
        spec["data_type"] = "options"
        cells = []
        for v in values:
            items = [x for x in (v if isinstance(v, list) else [v]) if not _blank(x)]
            cells.append(
                [{"text": _cell_text(x), "color": _tag_color(_cell_text(x), seen)} for x in items]
            )
        return spec, cells

    if kind == "markdown":
        spec["data_type"] = "markdown"
        return spec, [_cell_text(v) for v in values]
    if kind == "date":
        spec["data_type"] = "text"
        return spec, [_cell_text(v) for v in values]
    # text，以及数据不是数字的 number / money 列。
    spec["data_type"] = "lark_md"
    return spec, [_cell_text(v) for v in values]


def render_table(block: TableBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """显式的 table 块 → 表格组件。名额用完（每卡 5 个）返回空列表，由调用方降级。"""
    if not ctx.take_table():
        return []
    specs: list[Element] = []
    rows: list[dict[str, Any]] = [{} for _ in block.rows]
    for i, column in enumerate(block.columns):
        name = f"c{i}"
        values = [row.get(column.key) for row in block.rows]
        spec, cells = _table_column(column, name, values, ctx.people)
        specs.append(spec)
        for row, cell in zip(rows, cells, strict=True):
            if cell is not None and cell != []:
                row[name] = cell
    table: Element = {
        "tag": "table",
        "element_id": ctx.new_id("table"),
        "page_size": block.page_size or max(1, min(10, len(block.rows))),
        "row_height": "auto",
        "header_style": dict(TABLE_HEADER_STYLE),
        "columns": specs,
        "rows": rows,
    }
    out: list[Element] = []
    if block.title:
        out.append(_caption(ctx, block.title))
    out.append(table)
    return out


# ---------------------------------------------------------------- 原生组件


_RAW_ID_PREFIX = {
    "markdown": "md",
    "interactive_container": "panel",
    "column_set": "cols",
    "column": "col",
    "collapsible_panel": "fold",
    "img_combination": "imgs",
    "person_list": "people",
}


def _quota(elements: list[Element], ctx: RenderContext) -> list[Element]:
    """按全卡名额去掉多余的表格与图表（raw 块也占名额）。"""
    out: list[Element] = []
    for node in elements:
        tag = node.get("tag")
        if tag == "table" and not ctx.take_table():
            continue
        if tag == "chart" and not ctx.take_chart():
            continue
        for key in ("elements", "columns"):
            child = node.get(key)
            if tag != "table" and isinstance(child, list):
                node[key] = _quota(child, ctx)
        out.append(node)
    return out


def _assign_ids(value: Any, ctx: RenderContext) -> None:
    """组件统一重新分配 element_id（作者写的可能与编译器分配的撞车）；文本对象不需要。"""
    if isinstance(value, dict):
        tag = value.get("tag")
        if isinstance(tag, str) and tag in sanitize.DISPLAY_TAGS:
            value["element_id"] = ctx.new_id(_RAW_ID_PREFIX.get(tag, tag))
        else:
            value.pop("element_id", None)
        for key, item in value.items():
            if key not in {"chart_spec", "rows"}:
                _assign_ids(item, ctx)
    elif isinstance(value, list):
        for item in value:
            _assign_ids(item, ctx)


def render_raw(block: RawBlock, ctx: RenderContext) -> list[dict[str, Any]]:
    """技能作者写的原生组件：按白名单裁剪、占名额、重新分配 element_id；全被裁掉返回空列表。"""
    elements = _quota(sanitize.clean_elements(list(block.elements)), ctx)
    _assign_ids(elements, ctx)
    return elements
