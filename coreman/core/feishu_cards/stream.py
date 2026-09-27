"""流式期间的增量布局：正文逐字打出，块写完的那一刻整块插进卡片。

每次推送把当前正文算成一串「单元」，和卡片上已有的布局比较，得出最少的操作：
- 最后一段正文变长：只调流式文本接口（打字机效果要求旧文本是新文本的前缀）；
- 新写完一个块、占位换成块、两个半宽块凑成一行：一次 batch_update 删掉变化起点之后的
  旧组件、插入新组件。单元只会在末尾追加或变化，前面的不动，所以操作都很小。

标题栏（header）要整卡更新才能改，流式期间不显示，收尾整卡替换时出现。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from coreman.core.feishu_cards.compile import (
    _HALF,
    PENDING_LABELS,
    _Builder,
    _degraded,
)
from coreman.core.feishu_cards.components import two_columns
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.logging import get_logger
from coreman.core.richtext.blocks import split
from coreman.core.richtext.schema import HeaderBlock

log = get_logger(__name__)
# 第一段正文沿用流式卡片创建时的 answer 组件。
FIRST_TEXT_ID = "answer"
# 流式期间整卡的体积预算：超了就停止插入新块、停止加长正文，收尾时再整卡换成完整的富卡片。
STREAM_BUDGET = 24_000
# 单段正文在流式期间最多显示这么多（字节），和旧行为一致。
TEXT_LIMIT = 16_000

UnitKind = Literal["text", "block"]


@dataclass(frozen=True)
class StreamUnit:
    """一个单元：text 是一段可以打字的正文（一个 markdown 组件）；block 是整块出现的组件组。"""

    kind: UnitKind
    ids: tuple[str, ...]
    text: str = ""
    elements: tuple[dict[str, Any], ...] = ()
    digest: str = ""

    def record(self) -> dict[str, Any]:
        return {"kind": self.kind, "ids": list(self.ids), "text": self.text, "digest": self.digest}


@dataclass
class Plan:
    """一次推送要做的事：batch_update 的 actions（可能为空）与各段正文的流式更新。"""

    actions: list[dict[str, Any]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)
    layout: list[dict[str, Any]] = field(default_factory=list)
    frozen: bool = False


def _digest(elements: list[dict[str, Any]]) -> str:
    raw = json.dumps(elements, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _renamed(elements: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """组件 id 按单元位置重新编号：位置不变 id 就不变，同一张卡里也不会撞号。"""
    counter = [0]

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if "element_id" in out:
                out["element_id"] = f"{prefix}_{counter[0]}"
                counter[0] += 1
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return [walk(e) for e in elements]


def _text_id(index: int) -> str:
    return FIRST_TEXT_ID if index == 0 else f"s{index}_t"


def stream_units(text: str, *, allow_reply: bool = False) -> list[StreamUnit]:
    """流式正文 → 单元列表。正文段原样交给打字机（不做规范化，保证前缀稳定）。"""
    ctx = RenderContext(allow_reply=allow_reply)
    units: list[StreamUnit] = []
    half: tuple[list[dict[str, Any]], str] | None = None

    def add_block(elements: list[dict[str, Any]], digest: str) -> None:
        index = len(units)
        named = _renamed(elements, f"s{index}")
        units.append(
            StreamUnit(
                "block",
                tuple(e["element_id"] for e in named),
                elements=tuple(named),
                digest=digest,
            )
        )

    def flush_half() -> None:
        nonlocal half
        if half is not None:
            add_block(*half)
            half = None

    for seg in split(text, final=False):
        if seg.kind == "markdown":
            flush_half()
            units.append(StreamUnit("text", (_text_id(len(units)),), text=seg.text))
            continue
        if seg.kind == "pending":
            if seg.block and "header".startswith(seg.block):
                continue  # 标题栏流式期间不显示，也不闪占位
            flush_half()
            label = PENDING_LABELS.get(seg.block or "", "⏳ 正在生成…")
            element = {
                "tag": "markdown",
                "element_id": "p",
                "content": f"<font color='grey'>{label}</font>",
            }
            add_block([element], f"pending:{label}")
            continue
        if seg.kind == "invalid":
            flush_half()
            element = {"tag": "markdown", "element_id": "x", "content": f"```json\n{seg.text}\n```"}
            add_block([element], _digest([element]))
            continue
        if seg.model is None or not seg.block or isinstance(seg.model, HeaderBlock):
            continue
        elements, is_half = _block_elements(seg.block, seg.model, seg.text, ctx)
        if not elements:
            continue
        if is_half and half is not None:
            row = [two_columns(half[0], elements, ctx)]
            half = None
            add_block(row, _digest(row))
        elif is_half:
            flush_half()
            half = (elements, _digest(elements))
        else:
            flush_half()
            add_block(elements, _digest(elements))
    flush_half()
    return units


def _block_elements(
    kind: str, model: Any, raw: str, ctx: RenderContext
) -> tuple[list[dict[str, Any]], bool]:
    """复用终稿的块渲染（含降级），保证流式期间看到的和收尾后一致。"""
    builder = _Builder(ctx)
    try:
        builder.block(kind, model, raw)
    except Exception:  # noqa: BLE001 渲染出错只降级这一块
        log.warning("feishu_stream_block_failed", exc_info=True)
        builder.add_markdown(_degraded(kind, model, raw))
    builder.finish()
    elements = [e for unit in builder.units for e in unit.elements]
    is_half = getattr(model, "size", "full") == "half" and kind in _HALF
    return elements, is_half and len(builder.units) == 1


def plan(
    layout: list[dict[str, Any]],
    units: list[StreamUnit],
    *,
    anchor: str,
    shown: int,
    show: Callable[[str], str] = lambda text: text,
) -> Plan:
    """比较卡片上已有的布局和新单元，得出操作。

    Args:
        layout: 上次推送后记下的单元（`StreamUnit.record()` 的列表）
        units: 这次算出的单元
        anchor: 第一个单元前面那个组件的 id（思考面板）
        shown: 卡片除单元以外的体积（字节）；加上单元超过流式预算就冻结，不再加长或插入
        show: 正文段显示前的处理（如远程图片改成链接）；布局里记的是处理前的原文
    """
    same = 0
    while same < min(len(layout), len(units)):
        old, new = layout[same], units[same]
        if old.get("kind") != new.kind or list(old.get("ids") or []) != list(new.ids):
            break
        if new.kind == "block" and old.get("digest") != new.digest:
            break
        same += 1
    out = Plan()
    budget = STREAM_BUDGET - shown
    used = sum(_unit_size(u) for u in units[:same])
    kept: list[dict[str, Any]] = []
    for index in range(same):
        record, unit = layout[index], units[index]
        if unit.kind != "text" or unit.text == record.get("text"):
            kept.append(unit.record())
            continue
        grown = used - len(str(record.get("text") or "").encode()) + len(unit.text.encode())
        if grown > budget:
            # 放不下了：卡片上保持旧文本，收尾整卡替换时再完整显示。
            out.frozen = True
            kept.append(record)
            continue
        out.texts.append((unit.ids[0], _clip(show(unit.text)) or "…"))
        kept.append(unit.record())
    added: list[StreamUnit] = []
    for unit in units[same:]:
        if used + _unit_size(unit) > budget:
            out.frozen = True
            break
        added.append(unit)
        used += _unit_size(unit)
    removed = [i for record in layout[same:] for i in (record.get("ids") or [])]
    if removed:
        out.actions.append({"action": "delete_elements", "params": {"element_ids": removed}})
    if added:
        elements: list[dict[str, Any]] = []
        for unit in added:
            if unit.kind == "text":
                content = _clip(show(unit.text)) or "…"
                elements.append({"tag": "markdown", "element_id": unit.ids[0], "content": content})
            else:
                elements.extend(unit.elements)
        target = kept[-1]["ids"][-1] if kept else anchor
        out.actions.append(
            {
                "action": "add_elements",
                "params": {
                    "type": "insert_after",
                    "target_element_id": target,
                    "elements": elements,
                },
            }
        )
    out.layout = kept + [u.record() for u in added]
    return out


def _unit_size(unit: StreamUnit) -> int:
    return len(unit.text.encode()) + len(
        json.dumps(list(unit.elements), ensure_ascii=False).encode()
    )


def _clip(text: str) -> str:
    data = text.encode()
    if len(data) <= TEXT_LIMIT:
        return text
    return data[:TEXT_LIMIT].decode("utf-8", "ignore")
