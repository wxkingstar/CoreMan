"""把一条回复（GFM Markdown + ```card:类型``` 块）编译成飞书卡片 JSON 2.0。

流程：切块 → 各块交给对应渲染器 → 相邻的半宽块并排 → 按飞书上限装箱（元素 200、30KB、
表格 5、图表 5），装不下的放进续卡 → 自检。任何一块渲染不了（包括渲染器自身出错）都降级成
它的 Markdown，不让一个块拖垮整张卡；整张卡自检不过时由调用方改用 `simple_cards`。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.charts import render_chart
from coreman.core.feishu_cards.components import (
    columns,
    panel,
    render_actions,
    render_callout,
    render_header,
    render_item,
    render_kpi,
    render_note,
    render_people,
    render_raw,
    render_table,
    render_timeline,
    two_columns,
)
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.markdown import image_urls as markdown_image_urls
from coreman.core.feishu_cards.markdown import render_markdown
from coreman.core.feishu_cards.sanitize import validate_card
from coreman.core.logging import get_logger
from coreman.core.richtext.blocks import Segment, split
from coreman.core.richtext.degrade import block_to_markdown, strip_feishu_inline, to_markdown
from coreman.core.richtext.schema import (
    CalloutBlock,
    ChartBlock,
    ColumnsBlock,
    HeaderBlock,
    ItemBlock,
    KpiBlock,
    MarkdownChild,
    kind_of,
)

# 留出余量：飞书按请求体算 30KB，外层还要包一层 JSON 转义。
MAX_CARD_BYTES = 28_000
MAX_ELEMENTS = 190
# 单个 markdown 组件的上限；再长就按段落拆成多个组件。
MAX_MARKDOWN_BYTES = 12_000
SUMMARY_CHARS = 60
PENDING_LABELS = {
    "chart": "📊 正在生成图表…",
    "table": "📋 正在生成表格…",
    "kpi": "📈 正在整理指标…",
    "timeline": "🕒 正在整理进度…",
}
_MD_NOISE = re.compile(r"[*_`#>~|]|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|<[^>]+>")
_HALF = frozenset({"chart", "kpi", "callout"})


@dataclass
class Compiled:
    """cards[0] 是主卡（原地替换流式卡片），其余是紧接着发出的续卡。

    texts[i] 是第 i 张卡的 Markdown 降级文本：某张卡被飞书拒绝时只把这一张改发成普通消息，
    不重发整条回复。
    """

    cards: list[dict[str, Any]]
    summary: str
    texts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rich: bool = False


def image_urls(text: str) -> list[str]:
    """回复里所有需要先上传换 img_key 的远程图片：正文图片加 item 块的 image。"""
    urls: list[str] = []
    for seg in split(text, final=True):
        if seg.kind == "markdown":
            urls.extend(markdown_image_urls(seg.text))
        elif seg.kind == "block" and isinstance(seg.model, ItemBlock) and seg.model.image:
            urls.append(seg.model.image)
        elif seg.kind == "block" and isinstance(seg.model, ColumnsBlock):
            for col in seg.model.columns:
                for child in col:
                    if isinstance(child, ItemBlock) and child.image:
                        urls.append(child.image)
                    elif isinstance(child, MarkdownChild):
                        urls.extend(markdown_image_urls(child.text))
    return [u for u in dict.fromkeys(urls) if u.startswith(("http://", "https://"))]


def summary_of(text: str) -> str:
    """消息列表预览：第一句有内容的纯文本。"""
    for seg in split(text, final=True):
        if seg.kind == "block" and isinstance(seg.model, HeaderBlock):
            return seg.model.title[:SUMMARY_CHARS]
        if seg.kind != "markdown":
            continue
        for line in strip_feishu_inline(seg.text).splitlines():
            plain = _MD_NOISE.sub(lambda m: m.group(1) or "", line).strip(" -:：\t")
            if plain and not plain.startswith("```"):
                return plain[:SUMMARY_CHARS]
    return ""


def card_shell(
    elements: list[dict[str, Any]],
    *,
    header: dict[str, Any] | None = None,
    summary: str = "",
    streaming: bool = False,
    streaming_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """卡片外壳：共享卡片、撑满宽度、半透明面板色、统一留白。流式卡片和终稿同一套外壳，
    结束时整卡替换不会出现宽度或边距跳动。"""
    config: dict[str, Any] = {
        "update_multi": True,
        "width_mode": "fill",
        "style": style.card_style(),
    }
    if streaming:
        config["streaming_mode"] = True
        if streaming_config:
            config["streaming_config"] = streaming_config
    if summary:
        config["summary"] = {"content": summary}
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": config,
        "body": {
            "padding": style.BODY_PADDING,
            "vertical_spacing": style.BODY_SPACING,
            "elements": elements,
        },
    }
    if header:
        card["header"] = header
    return card


def count_elements(node: Any) -> int:
    """飞书按「元素或组件」计数：所有带 tag 的节点，包括 plain_text 这类元素。"""
    if isinstance(node, dict):
        return int("tag" in node) + sum(count_elements(v) for v in node.values())
    if isinstance(node, list):
        return sum(count_elements(v) for v in node)
    return 0


def _size(node: Any) -> int:
    return len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode())


def _cost(text: str) -> int:
    """文字放进卡片 JSON 后占的字节数（引号、换行、反斜杠都要转义）。"""
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


log = get_logger(__name__)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass
class _Unit:
    """一组必须放在同一张卡里的组件，以及它的 Markdown 降级文本。"""

    elements: list[dict[str, Any]]
    text: str


def _element_text(element: dict[str, Any]) -> str:
    """正文拆出来的组件还原成 Markdown（卡片被拒改发普通消息时用）。"""
    tag = element.get("tag")
    if tag == "markdown":
        return str(element.get("content") or "")
    if tag == "hr":
        return "---"
    if tag == "img":
        alt = ((element.get("alt") or {}).get("content")) or ""
        return f"![{alt}]({element.get('img_key')})"
    if tag == "img_combination":
        return "\n\n".join(f"![]({i.get('img_key')})" for i in element.get("img_list") or [])
    if tag == "table":
        cols = element.get("columns") or []
        names = [str(c.get("name")) for c in cols]
        rows = [
            "| " + " | ".join(str(r.get(n, "")).replace("|", "\\|") for n in names) + " |"
            for r in element.get("rows") or []
        ]
        header = "| " + " | ".join(str(c.get("display_name") or "") for c in cols) + " |"
        return "\n".join([header, "|" + "---|" * len(cols), *rows])
    return ""


def _markdown_units(text: str, ctx: RenderContext) -> list[_Unit]:
    """正文转组件，每个组件一个单元；过长的 markdown 组件按段落与代码块拆开。"""
    units: list[_Unit] = []
    for element in render_markdown(text, ctx):
        content = element.get("content")
        if element.get("tag") == "markdown" and isinstance(content, str):
            for chunk in _chunks(content, MAX_MARKDOWN_BYTES):
                piece = {**element, "element_id": ctx.new_id("md"), "content": chunk}
                units.append(_Unit([piece], chunk))
            continue
        units.append(_Unit([element], _element_text(element)))
    return units


def _chunks(text: str, limit: int) -> list[str]:
    """按段落装箱，每段不超过 limit（按转义后的字节）。围栏代码块整体算一段；
    超长的代码块在行边界切开，并在每一片前后补上围栏，不会切出半个代码块。"""
    if _cost(text) <= limit:
        return [text] if text.strip() else []
    out: list[str] = []
    current = ""
    for block in _paragraphs(text):
        pieces = [block] if _cost(block) <= limit else _split_block(block, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if _cost(candidate) <= limit:
                current = candidate
                continue
            if current:
                out.append(current)
            current = piece
    if current.strip():
        out.append(current)
    return out


def _paragraphs(text: str) -> list[str]:
    """空行分段；围栏代码块（含其中的空行）是一段。"""
    blocks: list[str] = []
    lines: list[str] = []
    marker: str | None = None
    for line in text.split("\n"):
        if marker is None:
            opened = _FENCE.match(line)
            if opened:
                if lines:
                    blocks.append("\n".join(lines))
                lines, marker = [line], opened.group(1)
                continue
            if not line.strip():
                if lines:
                    blocks.append("\n".join(lines))
                lines = []
                continue
            lines.append(line)
            continue
        lines.append(line)
        body = line.strip()
        if body and body[0] == marker[0] and len(body) >= len(marker) and set(body) == {marker[0]}:
            blocks.append("\n".join(lines))
            lines, marker = [], None
    if lines:
        blocks.append("\n".join(lines))
    return blocks


def _split_block(block: str, limit: int) -> list[str]:
    lines = block.split("\n")
    opened = _FENCE.match(lines[0])
    if opened is None:
        return _pack_lines(lines, limit)
    marker = opened.group(1)
    closing = marker[0] * len(marker)
    inner = lines[1:]
    if inner and inner[-1].strip() == closing:
        inner = inner[:-1]
    room = limit - _cost(lines[0]) - _cost(closing) - 4
    return [f"{lines[0]}\n{piece}\n{closing}" for piece in _pack_lines(inner, room)]


def _pack_lines(lines: list[str], limit: int) -> list[str]:
    """按行装箱；单行仍超限就按字符硬切。"""
    out: list[str] = []
    current: str | None = None
    for line in lines:
        for part in _hard_cut(line, limit):
            candidate = part if current is None else f"{current}\n{part}"
            if _cost(candidate) <= limit:
                current = candidate
                continue
            if current is not None:
                out.append(current)
            current = part
    if current is not None and current.strip():
        out.append(current)
    return out


def _hard_cut(line: str, limit: int) -> list[str]:
    parts: list[str] = []
    while _cost(line) > limit:
        cut = max(1, min(len(line), limit // 4))
        while cut < len(line) and _cost(line[: cut + 1]) <= limit:
            cut += max(1, (limit - _cost(line[:cut])) // 4)
        while cut > 1 and _cost(line[:cut]) > limit:
            cut -= 1
        parts.append(line[:cut])
        line = line[cut:]
    parts.append(line)
    return parts


def _degraded(kind: str, model: BaseModel, raw: str) -> str:
    """块的 Markdown 降级；降级器自身出错时退回块原文，信息不丢。"""
    try:
        return block_to_markdown(kind, model)
    except Exception:  # noqa: BLE001 降级是兜底路径，不能再抛
        log.warning("feishu_card_degrade_failed", exc_info=True)
        return f"```json\n{raw}\n```"


class _Builder:
    """把片段渲染成单元；半宽块两两并排。"""

    def __init__(self, ctx: RenderContext) -> None:
        self.ctx = ctx
        self.units: list[_Unit] = []
        self.header: dict[str, Any] | None = None
        self.rich = False
        self._half: _Unit | None = None

    def add(self, unit: _Unit) -> None:
        self._flush_half()
        if unit.elements:
            self.units.append(unit)

    def add_markdown(self, text: str) -> None:
        self._flush_half()
        self.units.extend(_markdown_units(text, self.ctx))

    def add_half(self, unit: _Unit) -> None:
        """半宽块：攒到两个就并排成一行；落单的按全宽放。"""
        if self._half is None:
            self._half = unit
            return
        row = two_columns(self._half.elements, unit.elements, self.ctx)
        self.units.append(_Unit([row], f"{self._half.text}\n\n{unit.text}"))
        self._half = None

    def _flush_half(self) -> None:
        if self._half is not None:
            self.units.append(self._half)
            self._half = None

    def finish(self) -> None:
        self._flush_half()

    def segment(self, seg: Segment, *, final: bool) -> None:
        if seg.kind == "markdown":
            self.add_markdown(seg.text)
        elif seg.kind == "pending":
            if not final:
                label = PENDING_LABELS.get(seg.block or "", "⏳ 正在生成…")
                self.add_markdown(f"<font color='grey'>{label}</font>")
        elif seg.kind == "invalid":
            self.add_markdown(f"```json\n{seg.text.strip()}\n```")
        elif seg.model is not None and seg.block:
            try:
                self.block(seg.block, seg.model, seg.text)
            except Exception:  # noqa: BLE001 一个块渲染出错只降级这一块
                log.warning("feishu_card_block_failed", exc_info=True)
                self.add_markdown(_degraded(seg.block, seg.model, seg.text))

    def block(self, kind: str, model: BaseModel, raw: str) -> None:
        self.rich = True
        text = _degraded(kind, model, raw)
        if isinstance(model, HeaderBlock):
            if self.header is None:
                self.header = render_header(model)
            else:
                self.add_markdown(text)
            return
        if isinstance(model, ColumnsBlock):
            cols = [self._children(col) for col in model.columns]
            if all(cols):
                self.add(_Unit([columns(cols, self.ctx)], text))
            else:
                self.add_markdown(text)
            return
        half = getattr(model, "size", "full") == "half" and kind in _HALF
        elements = self.render(kind, model, half=half)
        if not elements:
            # 名额用完（表格、图表）或全被裁掉（raw）：降级成 Markdown，信息不丢。
            self.add_markdown(text)
        elif half:
            self.add_half(_Unit(elements, text))
        else:
            self.add(_Unit(elements, text))

    def _children(self, children: Sequence[BaseModel]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for child in children:
            if isinstance(child, MarkdownChild):
                out.extend(e for u in _markdown_units(child.text, self.ctx) for e in u.elements)
                continue
            kind = kind_of(child)
            rendered = self.render(kind, child, half=True)
            if not rendered:
                text = _degraded(kind, child, "")
                rendered = [e for u in _markdown_units(text, self.ctx) for e in u.elements]
            out.extend(rendered)
        return out

    def render(self, kind: str, model: BaseModel, *, half: bool) -> list[dict[str, Any]]:
        ctx = self.ctx
        if isinstance(model, ChartBlock):
            elements = render_chart(model, ctx, half=half)
            # 并排的图表各自放进圆角面板：两栏对齐、层次清楚（与设计稿一致）。
            return [panel(elements, ctx)] if half and elements else elements
        if isinstance(model, KpiBlock):
            return render_kpi(model, ctx)
        if isinstance(model, CalloutBlock):
            return render_callout(model, ctx)
        renderers: dict[str, Any] = {
            "table": render_table,
            "item": render_item,
            "timeline": render_timeline,
            "people": render_people,
            "note": render_note,
            "actions": render_actions,
            "raw": render_raw,
        }
        renderer = renderers.get(kind)
        return list(renderer(model, ctx)) if renderer else []


def _fit(unit: _Unit, ctx: RenderContext) -> list[_Unit]:
    """单个单元超过一张卡的容量：表格按行拆成几张同表头的表，其他降级成 Markdown 再拆。"""
    budget = MAX_CARD_BYTES - 2_000
    if _size(unit.elements) <= budget and count_elements(unit.elements) <= MAX_ELEMENTS:
        return [unit]
    if len(unit.elements) == 1 and unit.elements[0].get("tag") == "table":
        return _split_table(unit.elements[0], ctx, budget)
    return _markdown_units(unit.text, ctx) if unit.text.strip() else []


def _split_table(table: dict[str, Any], ctx: RenderContext, budget: int) -> list[_Unit]:
    shell = {k: v for k, v in table.items() if k != "rows"}
    base = _size(shell) + 16
    units: list[_Unit] = []
    rows: list[dict[str, Any]] = []
    used = base
    for row in table.get("rows") or []:
        size = _size(row) + 1
        if rows and used + size > budget:
            units.append(_table_unit(shell, rows, ctx))
            rows, used = [], base
        rows.append(row)
        used += size
    if rows:
        units.append(_table_unit(shell, rows, ctx))
    return units


def _table_unit(shell: dict[str, Any], rows: list[dict[str, Any]], ctx: RenderContext) -> _Unit:
    element = {
        **shell,
        "element_id": ctx.new_id("tbl"),
        "page_size": min(10, max(1, len(rows))),
        "rows": rows,
    }
    return _Unit([element], _element_text(element))


def _pack(
    units: list[_Unit], prefix: Sequence[dict[str, Any]]
) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """按元素数与体积装箱；prefix（思考面板）只放主卡。"""
    cards: list[tuple[list[dict[str, Any]], list[str]]] = [(list(prefix), [])]
    used_count = count_elements(cards[0][0])
    used_bytes = _size(cards[0][0])
    for unit in units:
        n, size = count_elements(unit.elements), _size(unit.elements)
        elements, texts = cards[-1]
        if elements and (used_count + n > MAX_ELEMENTS or used_bytes + size > MAX_CARD_BYTES):
            cards.append(([], []))
            used_count = used_bytes = 0
        cards[-1][0].extend(unit.elements)
        if unit.text.strip():
            cards[-1][1].append(unit.text)
        used_count += n
        used_bytes += size
    return [c for c in cards if c[0]] or [([], [])]


def compile_reply(
    text: str,
    *,
    prefix: Sequence[dict[str, Any]] = (),
    images: Mapping[str, str] | None = None,
    people: Mapping[str, str] | None = None,
    final: bool = True,
    summary: str | None = None,
) -> Compiled:
    """一条回复 → 若干张卡。`prefix` 是放在主卡最前面的组件（思考面板）。

    编译器自身出错也不抛：返回简化卡并在 problems 里注明，调用方照常发送。
    """
    try:
        return _compile(text, prefix, images or {}, people or {}, final, summary)
    except Exception as exc:  # noqa: BLE001 编译器的 bug 不能让回复发不出去
        log.warning("feishu_card_compile_failed", exc_info=True)
        cards = simple_cards(text, prefix=prefix, summary=summary)
        texts = [_card_text(card) for card in cards]
        return Compiled(
            cards, summary or "", texts, problems=[f"compile_failed: {type(exc).__name__}"]
        )


def _compile(
    text: str,
    prefix: Sequence[dict[str, Any]],
    images: Mapping[str, str],
    people: Mapping[str, str],
    final: bool,
    summary: str | None,
) -> Compiled:
    ctx = RenderContext(images=images, people=people)
    builder = _Builder(ctx)
    for seg in split(text, final=final):
        builder.segment(seg, final=final)
    builder.finish()
    units = [piece for unit in builder.units for piece in _fit(unit, ctx)]
    packed = _pack(units, prefix)
    brief = summary if summary is not None else summary_of(text)
    cards: list[dict[str, Any]] = []
    for index, (elements, _texts) in enumerate(packed):
        if not elements:
            elements = [{"tag": "markdown", "element_id": ctx.new_id("md"), "content": "…"}]
        cards.append(
            card_shell(
                elements,
                header=builder.header if index == 0 else None,
                summary=brief if index == 0 else "",
            )
        )
    texts = ["\n\n".join(t) for _, t in packed]
    if builder.header is not None and texts:
        title = (builder.header.get("title") or {}).get("content") or ""
        texts[0] = f"**{title}**\n\n{texts[0]}" if title else texts[0]
    problems = [f"card {i}: {p}" for i, card in enumerate(cards) for p in validate_card(card)]
    return Compiled(cards, brief, texts, problems, rich=builder.rich)


def _card_text(card: dict[str, Any]) -> str:
    return "\n\n".join(
        str(e.get("content") or "")
        for e in (card.get("body") or {}).get("elements") or []
        if e.get("tag") == "markdown"
    )


def plain_text(text: str) -> str:
    """整条回复的 Markdown 降级；降级器出错时原样返回。"""
    try:
        return to_markdown(text, final=True)
    except Exception:  # noqa: BLE001 兜底路径
        log.warning("feishu_card_plain_failed", exc_info=True)
        return text


def simple_cards(
    text: str,
    *,
    prefix: Sequence[dict[str, Any]] = (),
    summary: str | None = None,
) -> list[dict[str, Any]]:
    """降级卡：所有块转成 Markdown，只用 markdown 组件；一张放不下就分成几张，不截断。"""
    ctx = RenderContext()
    chunks = _chunks(plain_text(text), MAX_MARKDOWN_BYTES) or ["…"]
    brief = summary if summary is not None else _safe_summary(text)
    cards: list[list[dict[str, Any]]] = [list(prefix)]
    used = _size(cards[0])
    for chunk in chunks:
        element = {"tag": "markdown", "element_id": ctx.new_id("md"), "content": chunk}
        size = _size(element)
        if len(cards[-1]) > len(prefix) * (len(cards) == 1) and used + size > MAX_CARD_BYTES:
            cards.append([])
            used = 0
        cards[-1].append(element)
        used += size
    return [
        card_shell(elements, summary=brief if index == 0 else "")
        for index, elements in enumerate(cards)
    ]


def _safe_summary(text: str) -> str:
    try:
        return summary_of(text)
    except Exception:  # noqa: BLE001 预览文案只是锦上添花
        return ""


def streaming_text(text: str) -> str:
    """流式期间单个 markdown 组件里显示的正文：块先显示成可读的 Markdown，未写完的块显示占位。

    只在末尾追加内容时保持前缀不变，打字机效果才连贯；块闭合那一刻由占位换成内容是唯一一次改写。
    出错时原样返回正文，流式不能因为渲染问题中断。
    """
    try:
        parts: list[str] = []
        for seg in split(text, final=False):
            if seg.kind == "markdown":
                parts.append(seg.text)
            elif seg.kind == "pending":
                label = PENDING_LABELS.get(seg.block or "", "⏳ 正在生成…")
                parts.append(f"<font color='grey'>{label}</font>")
            elif seg.kind == "invalid":
                parts.append(f"```json\n{seg.text.strip()}\n```")
            elif seg.model is not None and seg.block:
                parts.append(_degraded(seg.block, seg.model, seg.text))
        return "\n\n".join(p for p in parts if p.strip())
    except Exception:  # noqa: BLE001 兜底路径
        log.warning("feishu_card_streaming_text_failed", exc_info=True)
        return text
