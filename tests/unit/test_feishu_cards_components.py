"""飞书富内容块 → Card 2.0 组件：结构、配色约定、element_id、嵌套深度与整卡合法性。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from coreman.core.feishu_cards import components as c
from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.sanitize import CONTAINERS, validate_card
from coreman.core.richtext.schema import (
    ActionsBlock,
    CalloutBlock,
    HeaderBlock,
    ItemBlock,
    KpiBlock,
    NoteBlock,
    PeopleBlock,
    RawBlock,
    TableBlock,
    TimelineBlock,
)

IMAGE = "https://example.com/bag.png"
_HARDCODED = re.compile(r"(?<!&)#[0-9a-fA-F]{3,8}\b|rgba?\(")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")


def _ctx() -> RenderContext:
    return RenderContext(
        images={IMAGE: "img_v3_demo"},
        people={"alice@example.com": "ou_alice", "bob": "ou_bob"},
        allow_reply=True,
    )


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _tags(value: Any) -> list[str]:
    return [str(n["tag"]) for n in _walk(value) if "tag" in n]


def _depth(value: Any) -> int:
    """组件树里一条路径上最多有几层容器。"""
    if isinstance(value, list):
        return max((_depth(v) for v in value), default=0)
    if not isinstance(value, dict):
        return 0
    inner = max((_depth(v) for v in value.values()), default=0)
    return inner + (1 if value.get("tag") in CONTAINERS else 0)


def _card(elements: list[dict[str, Any]], header: dict[str, Any] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "style": style.card_style()},
        "body": {
            "padding": style.BODY_PADDING,
            "vertical_spacing": style.BODY_SPACING,
            "elements": elements,
        },
    }
    if header is not None:
        card["header"] = header
    return card


def _assert_card_ok(elements: list[dict[str, Any]]) -> None:
    """公共约束：整卡合法、不写死颜色、element_id 唯一合规、嵌套 ≤5。"""
    assert validate_card(_card(elements)) == []
    assert not _HARDCODED.search(json.dumps(elements, ensure_ascii=False))
    ids = [n["element_id"] for n in _walk(elements) if "element_id" in n]
    assert len(ids) == len(set(ids))
    assert all(_ID.match(i) for i in ids)
    assert _depth(elements) <= 5


def _contents(value: Any) -> list[str]:
    return [n["content"] for n in _walk(value) if n.get("tag") == "markdown"]


# ---------------------------------------------------------------- header


def test_header_maps_title_tags_template_and_icon() -> None:
    block = HeaderBlock.model_validate(
        {
            "title": "日报",
            "subtitle": "数据截至 23:59",
            "tags": ["定时", {"text": "达标", "color": "green"}],
            "color": "green",
            "icon": "info_outlined",
        }
    )
    assert c.render_header(block) == {
        "title": {"tag": "plain_text", "content": "日报"},
        "subtitle": {"tag": "plain_text", "content": "数据截至 23:59"},
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": "定时"},
                "color": "neutral",
            },
            {"tag": "text_tag", "text": {"tag": "plain_text", "content": "达标"}, "color": "green"},
        ],
        "template": "green",
        "icon": {"tag": "standard_icon", "token": "info_outlined"},
    }


def test_header_drops_unknown_icon_and_keeps_minimal_shape() -> None:
    header = c.render_header(HeaderBlock(title="T", icon="no-such-icon_outlined"))
    assert header == {"title": {"tag": "plain_text", "content": "T"}}
    assert validate_card(_card([], header)) == []


# ---------------------------------------------------------------- panel / 栏


def test_panel_is_rounded_interactive_container_without_behaviors() -> None:
    ctx = _ctx()
    md = {"tag": "markdown", "element_id": "x1", "content": "a"}
    assert c.panel([md], ctx) == {
        "tag": "interactive_container",
        "element_id": "panel_1",
        "behaviors": [],
        "background_style": style.PANEL,
        "corner_radius": style.RADIUS,
        "padding": style.PANEL_PADDING,
        "vertical_spacing": "4px",
        "elements": [md],
    }
    bordered = c.panel([], ctx, bg="orange-50", border="orange-200", spacing="6px")
    assert bordered["has_border"] is True
    assert bordered["border_color"] == "orange-200"
    assert bordered["element_id"] == "panel_2"


def test_two_columns_stack_on_narrow_screens() -> None:
    ctx = _ctx()
    node = c.two_columns([], [], ctx, ratio=(2, 9))
    assert node["tag"] == "column_set"
    assert node["flex_mode"] == "stretch"
    assert node["horizontal_spacing"] == "12px"
    assert [col["weight"] for col in node["columns"]] == [2, 5]
    assert all(col["width"] == "weighted" for col in node["columns"])


def test_columns_accepts_two_or_three() -> None:
    ctx = _ctx()
    assert len(c.columns([[], [], []], ctx)["columns"]) == 3
    with pytest.raises(ValueError):
        c.columns([[]], ctx)
    with pytest.raises(ValueError):
        c.columns([[], [], [], []], ctx)


# ---------------------------------------------------------------- kpi


def _kpi(items: list[dict[str, Any]], **extra: Any) -> KpiBlock:
    return KpiBlock.model_validate({"items": items, **extra})


def test_kpi_tile_golden() -> None:
    ctx = _ctx()
    block = _kpi(
        [{"label": "浏览", "value": 1240, "delta": "+12%", "good": True, "note": "周环比"}]
    )
    [row] = c.render_kpi(block, ctx)
    assert row == {
        "tag": "column_set",
        "element_id": "cols_1",
        "flex_mode": "bisect",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "element_id": "col_1",
                "width": "weighted",
                "weight": 1,
                "elements": [
                    {
                        "tag": "interactive_container",
                        "element_id": "panel_1",
                        "behaviors": [],
                        "background_style": style.PANEL,
                        "corner_radius": style.RADIUS,
                        "padding": style.TILE_PADDING,
                        "vertical_spacing": "2px",
                        "elements": [
                            {
                                "tag": "markdown",
                                "element_id": "md_1",
                                "content": "**1,240**",
                                "text_size": style.TEXT_KPI,
                            },
                            {
                                "tag": "markdown",
                                "element_id": "md_2",
                                "content": "<font color='grey'>浏览</font>",
                                "text_size": style.TEXT_NOTE,
                            },
                            {
                                "tag": "markdown",
                                "element_id": "md_3",
                                "content": "<font color='green'>↑12%</font>  "
                                "<font color='grey'>周环比</font>",
                                "text_size": style.TEXT_NOTE,
                            },
                        ],
                    }
                ],
            }
        ],
    }


def test_kpi_delta_colour_follows_good_and_arrow_follows_sign() -> None:
    block = _kpi(
        [
            {"label": "退款率", "value": "2.1%", "delta": "+0.4pt", "good": False},
            {"label": "成本", "value": "3", "delta": "-8%", "good": True},
            {"label": "访客", "value": "9", "delta": "−5"},
            {"label": "库存", "value": "7", "delta": "0"},
        ]
    )
    text = "\n".join(_contents(c.render_kpi(block, _ctx())))
    assert f"<font color='{style.BAD}'>↑0.4pt</font>" in text
    assert f"<font color='{style.GOOD}'>↓8%</font>" in text
    assert f"<font color='{style.NEUTRAL}'>↓5</font>" in text
    assert f"<font color='{style.NEUTRAL}'>0</font>" in text


@pytest.mark.parametrize(
    ("count", "columns", "shape"),
    [(1, None, [1]), (4, None, [4]), (5, None, [3, 3]), (8, None, [4, 4]), (6, 2, [2, 2, 2])],
)
def test_kpi_rows(count: int, columns: int | None, shape: list[int]) -> None:
    items = [{"label": f"指标{i}", "value": str(i)} for i in range(count)]
    rows = c.render_kpi(_kpi(items, columns=columns), _ctx())
    assert [len(r["columns"]) for r in rows] == shape
    assert all(r["flex_mode"] == "bisect" for r in rows)
    tiles = [n for n in _walk(rows) if n.get("tag") == "interactive_container"]
    assert len(tiles) == count
    _assert_card_ok(rows)


def test_kpi_last_row_padded_with_empty_columns() -> None:
    items = [{"label": str(i), "value": str(i)} for i in range(5)]
    rows = c.render_kpi(_kpi(items), _ctx())
    assert rows[1]["columns"][-1]["elements"] == []


def test_kpi_half_returns_stacked_tiles_without_column_set() -> None:
    items = [{"label": "a", "value": "1"}, {"label": "b", "value": "2"}]
    tiles = c.render_kpi(_kpi(items, size="half"), _ctx())
    assert [t["tag"] for t in tiles] == ["interactive_container", "interactive_container"]
    assert "column_set" not in _tags(tiles)
    _assert_card_ok([c.two_columns(tiles, [], _ctx())])


# ---------------------------------------------------------------- callout / note


@pytest.mark.parametrize("level", ["info", "success", "warning", "danger"])
def test_callout_uses_level_colours_and_icon(level: str) -> None:
    ctx = _ctx()
    [node] = c.render_callout(CalloutBlock(level=level, title="标题", text="正文 **重点**"), ctx)
    spec = style.LEVELS[level]
    assert node["tag"] == "interactive_container"
    assert node["background_style"] == spec["bg"]
    assert node["border_color"] == spec["border"]
    assert node["vertical_spacing"] == "6px"
    title, body = node["elements"]
    assert title["content"] == "**标题**"
    assert title["icon"] == {"tag": "standard_icon", "token": spec["icon"], "color": spec["text"]}
    assert body["content"] == "正文 **重点**"
    assert "icon" not in body
    _assert_card_ok([node])


def test_callout_without_title_puts_icon_on_text() -> None:
    [node] = c.render_callout(CalloutBlock(level="danger", text="失败"), _ctx())
    [body] = node["elements"]
    assert body["icon"]["token"] == style.LEVELS["danger"]["icon"]


def test_note_golden() -> None:
    assert c.render_note(NoteBlock(text="口径：已支付订单"), _ctx()) == [
        {
            "tag": "div",
            "element_id": "note_1",
            "icon": {"tag": "standard_icon", "token": "info_outlined", "color": "grey"},
            "text": {
                "tag": "plain_text",
                "content": "口径：已支付订单",
                "text_size": style.TEXT_NOTE,
                "text_color": "grey",
            },
        }
    ]


# ---------------------------------------------------------------- item


def _item(**extra: Any) -> ItemBlock:
    return ItemBlock.model_validate(
        {
            "title": "经典托特包 · 黑色",
            "eyebrow": "示例品牌",
            "meta": ["2024", "售价 $1,299"],
            "highlight": "已上架 72 天",
            "code": "SKU-001",
            "tags": ["新品", {"text": "热卖", "color": "red"}],
            **extra,
        }
    )


def test_item_with_image_golden_image_column() -> None:
    ctx = _ctx()
    [node] = c.render_item(_item(image=IMAGE), ctx)
    assert node["tag"] == "interactive_container"
    assert node["behaviors"] == []
    [row] = node["elements"]
    assert row["tag"] == "column_set"
    assert row["flex_mode"] == "none"
    left, right = row["columns"]
    assert left["width"] == "auto"
    assert left["elements"] == [
        {
            "tag": "img",
            "element_id": "img_1",
            "img_key": "img_v3_demo",
            "alt": {"tag": "plain_text", "content": "经典托特包 · 黑色"},
            "scale_type": "crop_center",
            "size": "72px 72px",
            "corner_radius": style.RADIUS,
            "preview": True,
        }
    ]
    assert right["width"] == "weighted"
    assert _contents(right) == [
        "<font color='grey'>示例品牌</font>",
        "**经典托特包 · 黑色**",
        "<font color='grey'>2024 · 售价 $1,299 · </font><font color='orange'>已上架 72 天</font>",
        "`SKU-001`",
        "<text_tag color='neutral'>新品</text_tag> <text_tag color='red'>热卖</text_tag>",
    ]
    sizes = [n["text_size"] for n in _walk(right) if n.get("tag") == "markdown"]
    assert sizes[1] == style.TEXT_TITLE
    _assert_card_ok([node])


def test_item_without_image_key_renders_text_only() -> None:
    for extra in ({}, {"image": "https://example.com/not-uploaded.png"}):
        [node] = c.render_item(_item(**extra), _ctx())
        assert "img" not in _tags(node)
        assert "column_set" not in _tags(node)
        assert _contents(node)[1] == "**经典托特包 · 黑色**"
        _assert_card_ok([node])


def test_item_link_makes_panel_clickable_only_for_http() -> None:
    [node] = c.render_item(_item(link="https://example.com/item/1"), _ctx())
    assert node["behaviors"] == [{"type": "open_url", "default_url": "https://example.com/item/1"}]
    for bad in ("javascript:alert(1)", "ftp://example.com/x", "/relative"):
        [node] = c.render_item(_item(link=bad), _ctx())
        assert node["behaviors"] == []


def test_item_highlight_level_and_escaping() -> None:
    block = ItemBlock.model_validate(
        {"title": "A<B>", "highlight": {"text": "缺货", "level": "danger"}}
    )
    contents = _contents(c.render_item(block, _ctx()))
    assert contents[0] == "**A&#60;B>**"
    assert contents[1] == "<font color='red'>缺货</font>"


# ---------------------------------------------------------------- timeline


def test_timeline_rows_with_time_column() -> None:
    block = TimelineBlock.model_validate(
        [
            {"time": "9/17", "text": "下单支付", "status": "done"},
            {"time": "9/20", "text": "清关中", "status": "warning"},
            {"text": "签收", "status": "pending"},
        ]
    )
    rows = c.render_timeline(block, _ctx())
    assert len(rows) == 3
    for row in rows:
        assert row["tag"] == "column_set"
        assert row["flex_mode"] == "none"
        assert row["columns"][0]["width"] == "96px"
        assert row["columns"][1]["width"] == "weighted"
    done_time, done_text = _contents(rows[0])
    assert done_time == "9/17"
    assert done_text == "下单支付"
    assert rows[0]["columns"][0]["elements"][0]["icon"] == {
        "tag": "standard_icon",
        "token": "yes_outlined",
        "color": "green",
    }
    warn_time, warn_text = _contents(rows[1])
    assert warn_time == "<font color='orange'>9/20</font>"
    assert warn_text == "<font color='orange'>清关中</font>"
    pending_time, pending_text = _contents(rows[2])
    assert pending_time == "&nbsp;"
    assert pending_text == "<font color='grey'>签收</font>"
    assert rows[2]["columns"][0]["elements"][0]["icon"]["token"] == "maybe_outlined"
    _assert_card_ok(rows)


def test_timeline_without_times_is_a_list_of_markdown() -> None:
    block = TimelineBlock.model_validate([{"text": "a"}, {"text": "b", "status": "current"}])
    rows = c.render_timeline(block, _ctx())
    assert [r["tag"] for r in rows] == ["markdown", "markdown"]
    assert rows[1]["content"] == "<font color='blue'>b</font>"
    assert rows[1]["icon"]["token"] == "time_outlined"


# ---------------------------------------------------------------- people


def test_people_person_list_and_unresolved_names() -> None:
    block = PeopleBlock(
        users=["alice@example.com", "bob", "alice@example.com", "carol"], title="负责人"
    )
    out = c.render_people(block, _ctx())
    assert [n["tag"] for n in out] == ["markdown", "person_list", "markdown"]
    assert out[0]["content"] == "**负责人**"
    assert out[1] == {
        "tag": "person_list",
        "element_id": "people_1",
        "persons": [{"id": "ou_alice"}, {"id": "ou_bob"}],
        "drop_invalid_user_id": True,
        "show_avatar": True,
        "show_name": True,
        "size": "small",
    }
    assert out[2]["content"] == "<font color='grey'>carol</font>"
    _assert_card_ok(out)


def test_people_without_mapping_is_text_only() -> None:
    out = c.render_people(PeopleBlock(users=["x@example.com", "y"]), _ctx())
    assert _tags(out) == ["markdown"]
    assert out[0]["content"] == "<font color='grey'>x@example.com、y</font>"


# ---------------------------------------------------------------- actions


def test_actions_buttons_row() -> None:
    block = ActionsBlock.model_validate(
        [
            {"text": "查看详情", "url": "https://example.com/order/1", "style": "primary"},
            {"text": "继续分析", "reply": "继续分析退款原因"},
            {"text": "撤销", "reply": "撤销", "style": "danger"},
            {"text": "坏链接", "url": "javascript:void(0)"},
        ]
    )
    [row] = c.render_actions(block, _ctx())
    assert row["tag"] == "column_set"
    assert row["flex_mode"] == "flow"
    assert all(col["width"] == "auto" for col in row["columns"])
    buttons = [col["elements"][0] for col in row["columns"]]
    assert [b["type"] for b in buttons] == ["primary_filled", "default", "danger"]
    assert all(b["size"] == "small" for b in buttons)
    assert buttons[0]["behaviors"] == [
        {"type": "open_url", "default_url": "https://example.com/order/1"}
    ]
    assert buttons[1]["behaviors"] == [
        {"type": "callback", "value": {"action": "reply", "reply": "继续分析退款原因"}}
    ]
    assert buttons[1]["text"] == {"tag": "plain_text", "content": "继续分析"}
    _assert_card_ok([row])


def test_reply_buttons_are_skipped_until_callbacks_are_supported() -> None:
    block = ActionsBlock.model_validate(
        [{"text": "打开", "url": "https://example.com"}, {"text": "继续", "reply": "继续"}]
    )
    [row] = c.render_actions(block, RenderContext())
    assert [col["elements"][0]["text"]["content"] for col in row["columns"]] == ["打开"]
    assert (
        c.render_actions(
            ActionsBlock.model_validate([{"text": "继续", "reply": "继续"}]), RenderContext()
        )
        == []
    )


def test_actions_all_invalid_links_render_nothing() -> None:
    block = ActionsBlock.model_validate([{"text": "x", "url": "mailto:a@example.com"}])
    assert c.render_actions(block, _ctx()) == []


# ---------------------------------------------------------------- table


def _table(**extra: Any) -> TableBlock:
    return TableBlock.model_validate(
        {
            "title": "近期订单",
            "columns": [
                {"key": "id", "title": "订单"},
                {"key": "qty", "title": "件数", "type": "number"},
                {"key": "amt", "title": "金额", "type": "money", "align": "right"},
                {"key": "rate", "title": "毛利率", "type": "percent"},
                {"key": "day", "title": "日期", "type": "date"},
                {"key": "owner", "title": "跟进", "type": "person"},
                {"key": "state", "title": "状态", "type": "tag"},
                {"key": "note", "title": "备注", "type": "markdown"},
            ],
            "rows": [
                {
                    "id": "A-1",
                    "qty": "1,200",
                    "amt": 1299.5,
                    "rate": 0.253,
                    "day": "2026-09-01",
                    "owner": "alice@example.com",
                    "state": "已完成",
                    "note": "**加急**",
                },
                {
                    "id": "A-2",
                    "qty": 3,
                    "amt": "88",
                    "rate": 0.1,
                    "day": "2026-09-02",
                    "owner": "bob",
                    "state": ["缺货", "预售"],
                    "note": None,
                },
            ],
            **extra,
        }
    )


def test_table_golden_columns_and_rows() -> None:
    ctx = _ctx()
    caption, table = c.render_table(_table(), ctx)
    assert caption["content"] == "**近期订单**"
    assert table["tag"] == "table"
    assert table["page_size"] == 2
    assert table["row_height"] == "auto"
    assert table["header_style"] == {
        "background_style": "grey",
        "text_color": "grey",
        "bold": True,
        "text_size": "normal",
    }
    assert table["columns"] == [
        {"name": "c0", "display_name": "订单", "data_type": "lark_md"},
        {
            "name": "c1",
            "display_name": "件数",
            "data_type": "number",
            "format": {"separator": True},
        },
        {
            "name": "c2",
            "display_name": "金额",
            "horizontal_align": "right",
            "data_type": "number",
            "format": {"separator": True, "symbol": "$", "precision": 2},
        },
        {
            "name": "c3",
            "display_name": "毛利率",
            "data_type": "lark_md",
            "horizontal_align": "right",
        },
        {"name": "c4", "display_name": "日期", "data_type": "text"},
        {"name": "c5", "display_name": "跟进", "data_type": "persons"},
        {"name": "c6", "display_name": "状态", "data_type": "options"},
        {"name": "c7", "display_name": "备注", "data_type": "markdown"},
    ]
    assert table["rows"][0] == {
        "c0": "A-1",
        "c1": 1200,
        "c2": 1299.5,
        "c3": "25.3%",
        "c4": "2026-09-01",
        "c5": ["ou_alice"],
        "c6": [{"text": "已完成", "color": "green"}],
        "c7": "**加急**",
    }
    assert table["rows"][1]["c6"] == [
        {"text": "缺货", "color": "orange"},
        {"text": "预售", "color": "blue"},
    ]
    assert table["rows"][1]["c7"] == ""
    _assert_card_ok([caption, table])


def test_table_type_fallbacks() -> None:
    block = TableBlock.model_validate(
        {
            "columns": [
                {"key": "n", "type": "number", "decimals": 1},
                {"key": "m", "type": "money", "prefix": "US$"},
                {"key": "p", "type": "percent", "decimals": 0},
                {"key": "o", "type": "person"},
            ],
            "rows": [
                {"n": "暂无", "m": 1234.5, "p": 25, "o": "someone@example.com"},
                {"n": 2, "m": "", "p": "12.5%", "o": "alice@example.com"},
            ],
        }
    )
    [table] = c.render_table(block, _ctx())
    types = [col["data_type"] for col in table["columns"]]
    assert types == ["lark_md", "lark_md", "lark_md", "text"]
    assert [r["c1"] for r in table["rows"]] == ["US$1,234.50", ""]
    assert [r["c2"] for r in table["rows"]] == ["25%", "12%"]
    assert table["rows"][0]["c3"] == "someone@example.com"
    assert table["rows"][0]["c0"] == "暂无"


def test_table_number_precision_and_page_size() -> None:
    rows = [{"v": i + 0.5} for i in range(15)]
    block = TableBlock.model_validate(
        {"columns": [{"key": "v", "type": "number", "decimals": 2}], "rows": rows}
    )
    [table] = c.render_table(block, _ctx())
    assert table["columns"][0]["format"] == {"separator": True, "precision": 2}
    assert table["page_size"] == 10
    assert table["rows"][0] == {"c0": 0.5}
    explicit = TableBlock.model_validate({**block.model_dump(), "page_size": 4})
    assert c.render_table(explicit, _ctx())[0]["page_size"] == 4


def test_table_quota_exhausted_returns_empty() -> None:
    ctx = _ctx()
    block = _table()
    for _ in range(5):
        assert c.render_table(block, ctx)
    assert c.render_table(block, ctx) == []


# ---------------------------------------------------------------- raw


def test_raw_is_cleaned_and_gets_fresh_ids() -> None:
    ctx = _ctx()
    block = RawBlock(
        elements=[
            {"tag": "markdown", "element_id": "md_1", "content": "自定义", "unknown": 1},
            {"tag": "form", "name": "f", "elements": []},
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "elements": [
                            {"tag": "markdown", "element_id": "md_1", "content": "嵌套"},
                            {"tag": "table", "columns": [{"name": "a"}], "rows": []},
                        ],
                    }
                ],
            },
        ]
    )
    c.render_note(NoteBlock(text="先占一个 id"), ctx)
    out = c.render_raw(block, ctx)
    assert _tags(out) == ["markdown", "column_set", "column", "markdown"]
    assert "unknown" not in out[0]
    _assert_card_ok([*c.render_note(NoteBlock(text="x"), ctx), *out])


def test_raw_charts_take_quota() -> None:
    ctx = _ctx()
    chart = {"tag": "chart", "chart_spec": {"type": "bar", "data": {"values": []}}}
    out = c.render_raw(RawBlock(elements=[dict(chart) for _ in range(7)]), ctx)
    assert len(out) == 5
    assert ctx.charts == 5


def test_raw_all_removed_returns_empty() -> None:
    block = RawBlock(elements=[{"tag": "input", "name": "x"}, {"tag": "audio", "file_key": "k"}])
    assert c.render_raw(block, _ctx()) == []


# ---------------------------------------------------------------- 整卡


def test_every_renderer_in_one_card_is_valid() -> None:
    ctx = _ctx()
    header = c.render_header(HeaderBlock(title="经营日报", color="turquoise"))
    kpi = _kpi([{"label": str(i), "value": str(i), "delta": "+1", "good": True} for i in range(4)])
    half = _kpi([{"label": "a", "value": "1"}], size="half")
    elements = [
        *c.render_kpi(kpi, ctx),
        *c.render_callout(CalloutBlock(level="warning", title="注意", text="库存偏低"), ctx),
        *c.render_item(_item(image=IMAGE, link="https://example.com/i"), ctx),
        *c.render_timeline(TimelineBlock.model_validate([{"time": "9/1", "text": "a"}]), ctx),
        *c.render_people(PeopleBlock(users=["bob", "dave"]), ctx),
        *c.render_note(NoteBlock(text="口径"), ctx),
        *c.render_actions(ActionsBlock.model_validate([{"text": "好", "reply": "好"}]), ctx),
        *c.render_table(_table(), ctx),
        *c.render_raw(RawBlock(elements=[{"tag": "hr"}]), ctx),
        c.two_columns(c.render_kpi(half, ctx), c.render_callout(CalloutBlock(text="t"), ctx), ctx),
        # 最深的组合：整行指标块与带图实体卡放进并排栏，正好 5 层容器。
        c.columns([c.render_kpi(kpi, ctx), c.render_item(_item(image=IMAGE), ctx)], ctx),
    ]
    card = _card(elements, header)
    assert validate_card(card) == []
    assert _depth(elements) == 5
    _assert_card_ok(elements)
