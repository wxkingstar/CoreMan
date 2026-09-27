"""把含富内容块的回复降级成普通 Markdown。

企业微信、管理台聊天记录、飞书卡片发送失败时的兜底都走这里。输出只用 GFM 的常见子集
（粗体、列表、引用、表格、链接、行内代码），企微 Markdown 和普通渲染器都能读；
块降级要保住数据本身，排版从简。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from typing import Any

from pydantic import BaseModel

from coreman.core.richtext.blocks import _LINE, _closes, _opening, has_blocks, split
from coreman.core.richtext.schema import (
    CARTESIAN,
    ITEMIZED,
    ActionsBlock,
    CalloutBlock,
    ChartBlock,
    ColumnsBlock,
    HeaderBlock,
    ItemBlock,
    KpiBlock,
    MarkdownChild,
    NoteBlock,
    NumberFormat,
    PeopleBlock,
    RawBlock,
    TableBlock,
    Tag,
    TimelineBlock,
)

MAX_CHART_ROWS = 12
MAX_TABLE_ROWS = 30

_LEVEL_EMOJI = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "danger": "❗"}
_STATUS_EMOJI = {"done": "✅", "current": "🔵", "warning": "⚠️", "error": "❌", "pending": "⚪"}
_NUMERIC_COLUMNS = frozenset({"number", "money", "percent"})


def to_markdown(text: str, *, final: bool = True) -> str:
    """整条回复降级：块换成 Markdown，飞书行内标记去掉标签留文字。

    流式中途（final=False）没写完的块不输出，免得露出半截 JSON；
    解析失败的块原样放进 json 代码块，信息不丢。
    """
    parts: list[str] = []
    for seg in split(text, final=final):
        if seg.kind == "markdown":
            parts.append(seg.text)
        elif seg.kind == "block" and seg.model is not None:
            parts.append(block_to_markdown(seg.block or "", seg.model))
        elif seg.kind == "invalid" and seg.text:
            parts.append(_code_block(seg.text, "json"))
    return strip_feishu_inline("\n\n".join(p for p in parts if p.strip()))


def block_to_markdown(kind: str, model: BaseModel) -> str:
    """单个块降级成 Markdown；按模型类型分派，kind 只是调用处的标注。

    这里不去掉飞书行内标记：飞书的简化卡片还认它们，要去掉的出口在外面调 strip_feishu_inline。
    """
    match model:
        case HeaderBlock():
            return _header(model)
        case KpiBlock():
            return _kpi(model)
        case ChartBlock():
            return _chart(model)
        case TableBlock():
            return _table(model)
        case CalloutBlock():
            return _callout(model)
        case ItemBlock():
            return _item(model)
        case TimelineBlock():
            return _timeline(model)
        case ColumnsBlock():
            return _columns(model)
        case PeopleBlock():
            title = f"{model.title}：" if model.title else ""
            return f"👥 {title}{'、'.join(model.users)}"
        case NoteBlock():
            return f"_注：{_one_line(model.text)}_"
        case ActionsBlock():
            return _actions(model)
        case RawBlock():
            return model.fallback or ""
        case MarkdownChild():
            return model.text
    return ""


# ---- 各块 ----


def _tags(tags: Sequence[Tag]) -> str:
    return "".join(f"【{t.text}】" for t in tags)


def _header(block: HeaderBlock) -> str:
    title = f"**{block.title}**"
    if block.tags:
        title += f" {_tags(block.tags)}"
    return f"{title}\n{block.subtitle}" if block.subtitle else title


def _arrow(delta: str) -> str:
    """方向只看 delta 的正负号；已经自带箭头、为零或没有数字（如「持平」）都不加。"""
    s = delta.strip()
    if not s or s[0] in "↑↓▲▼":
        return ""
    digits = re.sub(r"[^\d.]", "", s)
    try:
        if not digits or float(digits.strip(".") or "0") == 0:
            return ""
    except ValueError:
        return ""
    return "↓" if s[0] in "-−" else "↑"


def _kpi(block: KpiBlock) -> str:
    lines = []
    for item in block.items:
        extra = []
        if item.delta:
            arrow = _arrow(item.delta) if item.good is not None else ""
            extra.append(f"{arrow}{item.delta}")
        if item.note:
            extra.append(item.note)
        suffix = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"- **{item.label}**：{item.value}{suffix}")
    return "\n".join(lines)


def _chart(block: ChartBlock) -> str:
    parts = []
    if block.title:
        parts.append(f"**{block.title}**")
    if block.center:
        caption = f"{block.center.caption}：" if block.center.caption else ""
        parts.append(f"{caption}{block.center.value}")
    header, rows, numeric = _chart_rows(block)
    parts.append(_grid(header, rows, numeric, limit=MAX_CHART_ROWS))
    if block.summary:
        parts.append(block.summary)
    return "\n\n".join(parts)


def _chart_rows(block: ChartBlock) -> tuple[list[str], list[list[str]], list[bool]]:
    fmt, fmt2 = block.format, block.format2 or block.format
    if block.chart in CARTESIAN:
        header = [""]
        for i, s in enumerate(block.series, 1):
            header.append(s.name or ("数值" if len(block.series) == 1 else f"系列{i}"))
        formats = [fmt2 if s.axis == "right" else fmt for s in block.series]
        rows = [
            [x, *(_number(s.values[i], f) for s, f in zip(block.series, formats, strict=True))]
            for i, x in enumerate(block.x)
        ]
        return header, rows, [False] + [True] * len(block.series)
    if block.chart in ITEMIZED:
        rows = [[i.name, _number(i.value, fmt)] for i in block.items]
        return ["名称", "数值"], rows, [False, True]
    sizes = any(p.size is not None for p in block.points)
    groups = any(p.group for p in block.points)
    header = ["X", "Y"] + (["大小"] if sizes else []) + (["分组"] if groups else [])
    rows = []
    for p in block.points:
        row = [_number(p.x, None), _number(p.y, fmt)]
        if sizes:
            row.append(_number(p.size, None))
        if groups:
            row.append(p.group or "")
        rows.append(row)
    return header, rows, [True, True] + ([True] if sizes else []) + ([False] if groups else [])


def _table(block: TableBlock) -> str:
    header = [c.title or c.key for c in block.columns]
    rows = [
        [_cell(row.get(c.key), c.type, c.decimals, c.prefix) for c in block.columns]
        for row in block.rows
    ]
    numeric = [c.type in _NUMERIC_COLUMNS for c in block.columns]
    aligns = [c.align for c in block.columns]
    table = _grid(header, rows, numeric, limit=MAX_TABLE_ROWS, aligns=aligns)
    return f"**{block.title}**\n\n{table}" if block.title else table


def _callout(block: CalloutBlock) -> str:
    emoji = _LEVEL_EMOJI[block.level]
    lines = []
    if block.title:
        lines.append(f"{emoji} **{block.title}**")
    body = block.text.splitlines() if block.text else []
    if body and not lines:
        body[0] = f"{emoji} {body[0]}"
    lines.extend(body)
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def _item(block: ItemBlock) -> str:
    lines = []
    if block.image and _is_http(block.image):
        lines.append(f"![{_link_text(block.title)}]({block.image})")
    title = f"{block.eyebrow} · {block.title}" if block.eyebrow else block.title
    head = f"**{title}**"
    if block.tags:
        head += f" {_tags(block.tags)}"
    lines.append(head)
    if block.meta:
        lines.append(" · ".join(block.meta))
    detail = []
    if block.code:
        detail.append(_inline_code(block.code))
    if block.highlight:
        detail.append(f"{_LEVEL_EMOJI[block.highlight.level]} {block.highlight.text}")
    if detail:
        lines.append(" · ".join(detail))
    if block.link and _is_http(block.link):
        lines.append(f"[查看]({block.link})")
    return "\n".join(lines)


def _timeline(block: TimelineBlock) -> str:
    lines = []
    for step in block.steps:
        time = f"{step.time}  " if step.time else ""
        lines.append(f"- {_STATUS_EMOJI[step.status]} {time}{step.text}")
    return "\n".join(lines)


def _columns(block: ColumnsBlock) -> str:
    parts = []
    for column in block.columns:
        for child in column:
            text = block_to_markdown("", child)
            if text.strip():
                parts.append(text)
    return "\n\n".join(parts)


def _actions(block: ActionsBlock) -> str:
    out = []
    for b in block.buttons:
        if b.url and _is_http(b.url):
            out.append(f"[{_link_text(b.text)}]({b.url})")
        else:
            out.append(f"「{b.text}」")
    return " · ".join(out)


# ---- 格式 ----


def _plain_number(value: float, decimals: int) -> str:
    """整数加千分位；小数最多 decimals 位并去掉尾零。"""
    text = f"{value:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _number(value: float | None, fmt: NumberFormat | None) -> str:
    if value is None:
        return "-"
    decimals = 2 if fmt is None or fmt.decimals is None else fmt.decimals
    unit = (fmt.unit if fmt else None) or ""
    if fmt is not None and fmt.percent_input is not None:
        # 0.25 这种比例写法按百分比展示，免得读成 0.25%。
        value = value * 100 if fmt.percent_input == "ratio" else value
        unit = unit or "%"
    text = _plain_number(value, decimals)
    prefix = (fmt.prefix if fmt else None) or ""
    sign, text = ("-", text[1:]) if text.startswith("-") else ("", text)
    return f"{sign}{prefix}{text}{unit}"


def _cell(value: Any, kind: str, decimals: int | None, prefix: str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        text = _plain_number(float(value), 2 if decimals is None else decimals)
        sign, text = ("-", text[1:]) if text.startswith("-") else ("", text)
        suffix = "%" if kind == "percent" else ""
        return f"{sign}{prefix or ''}{text}{suffix}"
    if isinstance(value, list):
        return "、".join(_cell(v, "text", None, None) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _escape_cell(text: str) -> str:
    """表格单元格里的 | 会被当成分隔符、换行会断开这一行，都要处理。"""
    return re.sub(r"\s*\r?\n\s*", " ", text).replace("|", "\\|").strip()


def _grid(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    numeric: Sequence[bool],
    *,
    limit: int,
    aligns: Sequence[str | None] | None = None,
) -> str:
    """GFM 表格；超过 limit 行只列前面的，后面空一行注明总行数（紧跟表格的文字会被当成一行）。"""
    marks = []
    for i, is_number in enumerate(numeric):
        align = aligns[i] if aligns else None
        align = align or ("right" if is_number else None)
        marks.append({"left": ":---", "center": ":---:", "right": "---:"}.get(align or "", "---"))
    lines = [
        "| " + " | ".join(_escape_cell(h) for h in header) + " |",
        "| " + " | ".join(marks) + " |",
    ]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(_escape_cell(c) for c in row) + " |")
    table = "\n".join(lines)
    return f"{table}\n\n…共 {len(rows)} 行" if len(rows) > limit else table


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _link_text(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _inline_code(text: str) -> str:
    """行内代码：内容里有反引号时用更长的反引号串包住，两侧补空格（CommonMark 规则）。"""
    runs = [len(r) for r in re.findall(r"`+", text)]
    ticks = "`" * (max(runs, default=0) + 1)
    pad = " " if runs else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _code_block(text: str, lang: str) -> str:
    """围栏比内容里最长的反引号串还长，内容里的 ``` 不会提前关掉代码块。"""
    runs = [len(r) for r in re.findall(r"`+", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


# ---- 飞书行内标记 ----

_ATTRS = r"""(?:\s+[\w-]+\s*=\s*(?:'[^']*'|"[^"]*"|[^\s'">]+))*\s*"""
_FONT = re.compile(rf"<font{_ATTRS}>(.*?)</font\s*>", re.IGNORECASE | re.DOTALL)
_TEXT_TAG = re.compile(rf"<text_tag{_ATTRS}>(.*?)</text_tag\s*>", re.IGNORECASE | re.DOTALL)
_TICKS = re.compile(r"`+")


def _prose_and_code(md: str) -> Iterator[tuple[bool, str]]:
    """按顺序切出 (是否代码, 文字)：围栏代码块和行内代码算代码，原样保留。"""
    prose: list[str] = []
    fence = None
    for line in _LINE.findall(md):
        content = line.rstrip("\r\n")
        if fence is None:
            fence = _opening(content)
            if fence is None:
                prose.append(line)
                continue
            if prose:
                yield from _split_code_spans("".join(prose))
                prose.clear()
            yield True, line
        else:
            if _closes(content, fence):
                fence = None
            yield True, line
    if prose:
        yield from _split_code_spans("".join(prose))


def _split_code_spans(text: str) -> Iterator[tuple[bool, str]]:
    """行内代码：N 个反引号开头，到下一串恰好 N 个反引号结束；配不上的反引号只是普通字符。"""
    runs = list(_TICKS.finditer(text))
    # 每串反引号之后第一串等长的下标；倒着扫一遍，免得逐个往后找变成平方复杂度。
    partner: list[int | None] = [None] * len(runs)
    last: dict[int, int] = {}
    for i in range(len(runs) - 1, -1, -1):
        width = len(runs[i].group())
        partner[i] = last.get(width)
        last[width] = i
    i = start = 0
    while i < len(runs):
        j = partner[i]
        if j is None:
            i += 1
            continue
        if runs[i].start() > start:
            yield False, text[start : runs[i].start()]
        yield True, text[runs[i].start() : runs[j].end()]
        start = runs[j].end()
        i = j + 1
    if start < len(text):
        yield False, text[start:]


def _strip_tags(text: str) -> str:
    text = _TEXT_TAG.sub(lambda m: f"【{m.group(1)}】", text)
    # 嵌套的 <font> 每轮剥掉一层。
    for _ in range(8):
        stripped = _FONT.sub(lambda m: m.group(1), text)
        if stripped == text:
            break
        text = stripped
    return text


def strip_feishu_inline(md: str) -> str:
    """`<font color=…>文</font>` → `文`；`<text_tag color=…>文</text_tag>` → `【文】`。

    只认这两个标签，其余 HTML 原样；代码块和行内代码里的内容不动。
    """
    if "<" not in md:
        return md
    return "".join(
        chunk if is_code else _strip_tags(chunk) for is_code, chunk in _prose_and_code(md)
    )


def readable(text: str, *, final: bool = True) -> str:
    """不认卡片的出口（企微、管理台）用：含 card: 块才降级，其余原样返回，不动普通正文。

    降级出错时也原样返回：这些出口宁可显示原文，也不能因为一个坏块整条发不出去或整页报错。
    """
    try:
        return to_markdown(text, final=final) if has_blocks(text) else text
    except Exception:  # noqa: BLE001 兜底路径
        return text
