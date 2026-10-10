"""命令输出：列表打成对齐的表格，详情打成「字段: 值」，--json 时原样输出接口数据。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

Column = tuple[str, str | Callable[[dict[str, Any]], Any]]


def dump(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(text(v) for v in value) if value else "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str) if value else "-"
    return str(value)


def width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in value)


def cell(row: dict[str, Any], key: str | Callable[[dict[str, Any]], Any]) -> str:
    raw = key(row) if callable(key) else row.get(key)
    return " ".join(text(raw).split())


def table(
    rows: Sequence[dict[str, Any]], columns: Sequence[Column], *, empty: str = "（无）"
) -> None:
    if not rows:
        print(empty)
        return
    cells = [[cell(row, key) for _, key in columns] for row in rows]
    heads = [head for head, _ in columns]
    widths = [max(width(v) for v in [head, *(r[i] for r in cells)]) for i, head in enumerate(heads)]

    def line(values: list[str]) -> str:
        padded = [v + " " * (widths[i] - width(v)) for i, v in enumerate(values)]
        return "  ".join(padded).rstrip()

    print(line(heads))
    for values in cells:
        print(line(values))


def fields(data: dict[str, Any], spec: Sequence[Column]) -> None:
    pad = max(width(label) for label, _ in spec)
    for label, key in spec:
        raw = key(data) if callable(key) else data.get(key)
        print(f"{label}{' ' * (pad - width(label))}  {text(raw)}")


def enabled(row: dict[str, Any]) -> str:
    return "启用" if row.get("enabled") else "停用"
