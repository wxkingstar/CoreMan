"""回复里的富内容块：```` ```card:<类型> ```` 围栏代码块里那段 JSON 的数据模型。

模型只写数据和语义，颜色、尺寸、字号都由各平台渲染器决定。解析是宽松的：多余字段忽略，
数字写成字符串、单个对象写成数组这类常见笔误自动纠正；真正缺了必需内容才算格式错误，
由调用方把整块降级成原文展示，不能让一个块拖垮整条回复。
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Level = Literal["info", "success", "warning", "danger"]
Status = Literal["done", "current", "warning", "error", "pending"]
Size = Literal["full", "half"]
# 飞书标题栏 template 的色系；其他平台按语义近似。
HeaderColor = Literal[
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
]
TagColor = Literal[
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
]
ChartKind = Literal[
    "line",
    "area",
    "bar",
    "hbar",
    "pie",
    "donut",
    "funnel",
    "combo",
    "progress",
    "ring",
    "radar",
    "scatter",
]
# 图表按数据形状分三类，校验和渲染都按这个分。
CARTESIAN: frozenset[str] = frozenset({"line", "area", "bar", "combo", "radar"})
ITEMIZED: frozenset[str] = frozenset({"pie", "donut", "funnel", "hbar", "progress", "ring"})

_NUMBER_JUNK = re.compile(r"[,\s，%％$¥￥€£]")
MAX_CHART_POINTS = 500
MAX_TABLE_ROWS = 200


class BlockError(ValueError):
    """块内容不合约定；消息给日志看，不给用户看。"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, frozen=True)


def _as_list(value: Any) -> Any:
    """单个对象或字符串当成只有一项的数组。"""
    if value is None:
        return []
    if isinstance(value, (dict, str)):
        return [value]
    return value


def _number(value: Any) -> float | None:
    """`"1,240"`、`"12.5%"`、`"$4,621"` 都认成数字；空值与认不出的都是 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _bounded(value)
    if isinstance(value, str):
        text = _NUMBER_JUNK.sub("", value)
        if not text or text.lower() in {"null", "none", "nan", "-"}:
            return None
        try:
            return _bounded(float(text))
        except (ValueError, OverflowError):
            return None
    return None


def _bounded(value: float) -> float | None:
    """NaN、无穷和超出报表量级的数都按缺失处理：它们会让卡片 JSON 非法或让格式化溢出。"""
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and abs(number) <= 1e15 else None


def _text(value: Any) -> Any:
    """指标值这类展示用字段：数字格式化成带千分位的文字，其余原样交给 pydantic。"""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")
    return value


class Tag(_Model):
    text: str = Field(min_length=1, max_length=40)
    color: TagColor | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value


class HeaderBlock(_Model):
    title: str = Field(min_length=1, max_length=200)
    subtitle: str | None = Field(default=None, max_length=200)
    tags: list[Tag] = Field(default_factory=list, max_length=3)
    color: HeaderColor | None = None
    icon: str | None = None

    _list_tags = field_validator("tags", mode="before")(_as_list)


class KpiItem(_Model):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=40)
    delta: str | None = Field(default=None, max_length=40)
    # 这个变化是好是坏：True 绿、False 红、None 灰。方向（↑↓）取自 delta 的正负号。
    good: bool | None = None
    note: str | None = Field(default=None, max_length=80)

    _value_text = field_validator("value", "delta", mode="before")(_text)


class KpiBlock(_Model):
    items: list[KpiItem] = Field(min_length=1, max_length=8)
    columns: int | None = Field(default=None, ge=2, le=4)
    size: Size = "full"

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, value: Any) -> Any:
        return {"items": value} if isinstance(value, list) else value


class Series(_Model):
    name: str = ""
    values: list[float | None]
    kind: Literal["bar", "line"] | None = None
    axis: Literal["left", "right"] | None = None
    color: str | None = None

    @field_validator("values", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        return [_number(v) for v in _as_list(value)] if isinstance(value, (list, tuple)) else value


class ChartItem(_Model):
    name: str = Field(min_length=1, max_length=60)
    value: float
    color: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _pair(cls, value: Any) -> Any:
        # ["华东", 820] 这种二元组也认。
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return {"name": str(value[0]), "value": value[1]}
        if isinstance(value, dict) and "label" in value and "name" not in value:
            return {**value, "name": value["label"]}
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _num(cls, value: Any) -> Any:
        number = _number(value)
        return value if number is None else number


class ChartPoint(_Model):
    x: float
    y: float
    size: float | None = None
    group: str | None = None

    @field_validator("x", "y", "size", mode="before")
    @classmethod
    def _num(cls, value: Any) -> Any:
        number = _number(value)
        return value if number is None else number


class NumberFormat(_Model):
    unit: str | None = Field(default=None, max_length=10)
    prefix: str | None = Field(default=None, max_length=3)
    decimals: int | None = Field(default=None, ge=0, le=4)
    # 大数换算：auto 按量级自动用万/亿；none 原样。
    scale: Literal["auto", "none", "wan", "yi"] = "none"
    # 百分比数据是 0.25 还是 25。
    percent_input: Literal["ratio", "percent"] | None = None


class Center(_Model):
    value: str = Field(min_length=1, max_length=20)
    caption: str | None = Field(default=None, max_length=20)

    _value_text = field_validator("value", mode="before")(_text)


class ChartBlock(_Model):
    chart: ChartKind
    title: str | None = Field(default=None, max_length=80)
    x: list[str] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list, max_length=8)
    items: list[ChartItem] = Field(default_factory=list)
    points: list[ChartPoint] = Field(default_factory=list)
    stack: Literal["none", "stack", "percent"] = "none"
    format: NumberFormat | None = None
    format2: NumberFormat | None = None
    labels: Literal["auto", "none", "all", "last", "max"] = "auto"
    legend: Literal["auto", "none", "bottom"] = "auto"
    sort: Literal["none", "desc", "asc"] | None = None
    top_n: int | None = Field(default=None, ge=1, le=50)
    other_name: str = Field(default="其他", max_length=20)
    highlight: list[str] | Literal["last", "max"] | None = None
    max: float | None = None
    center: Center | None = None
    smooth: bool = True
    y_zero: bool | None = None
    colors: list[str] | None = None
    size: Size = "full"
    height: Literal["sm", "md", "lg"] | None = None
    summary: str | None = Field(default=None, max_length=200)

    @field_validator("x", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> Any:
        return [str(_text(v)) for v in value] if isinstance(value, (list, tuple)) else value

    @field_validator("series", "items", "points", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return _as_list(value)

    @model_validator(mode="after")
    def _shape(self) -> ChartBlock:
        if self.chart in CARTESIAN:
            if not self.x or not self.series:
                raise ValueError(f"{self.chart} 需要 x 和 series")
            for s in self.series:
                if len(s.values) != len(self.x):
                    raise ValueError("series.values 与 x 长度不一致")
            if len(self.x) * len(self.series) > MAX_CHART_POINTS:
                raise ValueError("数据点太多")
        elif self.chart in ITEMIZED:
            if not self.items:
                raise ValueError(f"{self.chart} 需要 items")
            if self.chart in {"pie", "donut"} and any(i.value < 0 for i in self.items):
                raise ValueError("饼图的值不能为负")
        elif not self.points:
            raise ValueError("scatter 需要 points")
        if len(self.items) > MAX_CHART_POINTS or len(self.points) > MAX_CHART_POINTS:
            raise ValueError("数据点太多")
        return self


class TableColumn(_Model):
    key: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=40)
    type: Literal["text", "number", "money", "percent", "date", "person", "tag", "markdown"] = (
        "text"
    )
    align: Literal["left", "center", "right"] | None = None
    decimals: int | None = Field(default=None, ge=0, le=4)
    prefix: str | None = Field(default=None, max_length=3)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, value: Any) -> Any:
        return {"key": value, "title": value} if isinstance(value, str) else value


class TableBlock(_Model):
    columns: list[TableColumn] = Field(min_length=1, max_length=20)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_TABLE_ROWS)
    page_size: int | None = Field(default=None, ge=1, le=10)
    title: str | None = Field(default=None, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def _positional_rows(cls, value: Any) -> Any:
        # rows 写成二维数组时按列顺序对上 key。
        if not isinstance(value, dict):
            return value
        columns, rows = value.get("columns"), value.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            keys = [
                c.get("key") if isinstance(c, dict) else c if isinstance(c, str) else str(c)
                for c in columns
            ]
            value = {
                **value,
                "rows": [
                    dict(zip(keys, row, strict=False)) if isinstance(row, (list, tuple)) else row
                    for row in rows
                ],
            }
        return value


class CalloutBlock(_Model):
    level: Level = "info"
    title: str | None = Field(default=None, max_length=80)
    text: str = Field(default="", max_length=2000)
    size: Size = "full"

    @model_validator(mode="after")
    def _not_empty(self) -> CalloutBlock:
        if not self.title and not self.text:
            raise ValueError("callout 需要 title 或 text")
        return self


class Highlight(_Model):
    text: str = Field(min_length=1, max_length=40)
    level: Level = "warning"

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value


class ItemBlock(_Model):
    title: str = Field(min_length=1, max_length=120)
    image: str | None = None
    eyebrow: str | None = Field(default=None, max_length=40)
    meta: list[str] = Field(default_factory=list, max_length=6)
    code: str | None = Field(default=None, max_length=60)
    highlight: Highlight | None = None
    tags: list[Tag] = Field(default_factory=list, max_length=3)
    link: str | None = None

    _lists = field_validator("meta", "tags", mode="before")(_as_list)


class TimelineStep(_Model):
    text: str = Field(min_length=1, max_length=200)
    time: str | None = Field(default=None, max_length=30)
    status: Status = "done"


class TimelineBlock(_Model):
    steps: list[TimelineStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, value: Any) -> Any:
        return {"steps": value} if isinstance(value, list) else value


class PeopleBlock(_Model):
    users: list[str] = Field(min_length=1, max_length=50)
    title: str | None = Field(default=None, max_length=80)

    _lists = field_validator("users", mode="before")(_as_list)


class NoteBlock(_Model):
    text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value


class Button(_Model):
    text: str = Field(min_length=1, max_length=40)
    url: str | None = None
    reply: str | None = Field(default=None, max_length=500)
    style: Literal["primary", "danger", "default"] = "default"

    @model_validator(mode="after")
    def _one_action(self) -> Button:
        if bool(self.url) == bool(self.reply):
            raise ValueError("按钮要么给 url，要么给 reply")
        return self


class ActionsBlock(_Model):
    buttons: list[Button] = Field(min_length=1, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, value: Any) -> Any:
        return {"buttons": value} if isinstance(value, list) else value


class RawBlock(_Model):
    elements: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    # 非飞书出口展示的文字；不给就在降级时整块省略。
    fallback: str | None = Field(default=None, max_length=4000)

    _lists = field_validator("elements", mode="before")(_as_list)


class MarkdownChild(_Model):
    text: str = Field(min_length=1)


class ColumnsBlock(_Model):
    """并排布局：每一栏是若干子块，子块是 `{"type": <类型>, ...}` 或 `{"type": "markdown"}`。

    子块不能是 table（飞书表格只能在卡片根节点）、columns（不再嵌套）、header、actions、raw。
    """

    columns: list[list[Block]] = Field(min_length=2, max_length=3)

    @field_validator("columns", mode="before")
    @classmethod
    def _children(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [[_child(c) for c in _as_list(col)] for col in value]


# 能出现在 columns 里的子块；解析结果仍是具体模型。
Block = (
    MarkdownChild
    | KpiBlock
    | ChartBlock
    | CalloutBlock
    | ItemBlock
    | TimelineBlock
    | PeopleBlock
    | NoteBlock
)
CHILD_KINDS: frozenset[str] = frozenset(
    {"markdown", "kpi", "chart", "callout", "item", "timeline", "people", "note"}
)

BLOCK_MODELS: dict[str, type[_Model]] = {
    "header": HeaderBlock,
    "kpi": KpiBlock,
    "chart": ChartBlock,
    "table": TableBlock,
    "callout": CalloutBlock,
    "item": ItemBlock,
    "timeline": TimelineBlock,
    "columns": ColumnsBlock,
    "people": PeopleBlock,
    "note": NoteBlock,
    "actions": ActionsBlock,
    "raw": RawBlock,
}
BLOCK_KINDS: frozenset[str] = frozenset(BLOCK_MODELS)


def _child(value: Any) -> _Model:
    if isinstance(value, str):
        return MarkdownChild(text=value)
    if not isinstance(value, dict):
        raise ValueError("columns 的子块必须是对象或文字")
    kind = str(value.get("type") or "")
    if kind not in CHILD_KINDS:
        raise ValueError(f"columns 里不能放 {kind or '未写类型'} 块")
    body = {k: v for k, v in value.items() if k != "type"}
    if kind == "markdown":
        return MarkdownChild(text=str(body.get("text") or ""))
    return BLOCK_MODELS[kind].model_validate(body)


ColumnsBlock.model_rebuild()


def validate_block(kind: str, data: Any) -> _Model:
    """把块 JSON 校验成模型；不认识的类型或内容不合约定抛 BlockError。"""
    model = BLOCK_MODELS.get(kind)
    if model is None:
        raise BlockError(f"未知的块类型 {kind!r}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or kind
        raise BlockError(f"{kind}.{where}: {first.get('msg')}") from exc


def kind_of(model: BaseModel) -> str:
    """模型对应的块类型名（columns 子块用）。"""
    if isinstance(model, MarkdownChild):
        return "markdown"
    for name, cls in BLOCK_MODELS.items():
        if type(model) is cls:
            return name
    raise KeyError(type(model).__name__)
