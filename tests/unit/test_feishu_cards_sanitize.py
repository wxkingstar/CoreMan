"""原生组件白名单：raw 块的裁剪（clean_elements）与整卡自检（validate_card）。"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.sanitize import (
    COLOR_NAMES,
    clean_elements,
    is_http_url,
    validate_card,
)


def _md(content: str = "文字", **extra: Any) -> dict[str, Any]:
    return {"tag": "markdown", "content": content, **extra}


def _box(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"tag": "interactive_container", "behaviors": [], "elements": list(elements)}


def _card(elements: list[Any], **extra: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True, "style": style.card_style()},
        "body": {"elements": elements},
    }
    card.update(extra)
    return card


def _problems(elements: list[Any], **extra: Any) -> str:
    return "\n".join(validate_card(_card(elements, **extra)))


# ---------------------------------------------------------------- clean_elements


def test_clean_keeps_valid_components_untouched() -> None:
    elements = [
        _md("**粗体**", element_id="md_a", text_size="heading", text_align="left"),
        {"tag": "hr", "margin": "8px 0"},
        {
            "tag": "div",
            "icon": {"tag": "standard_icon", "token": "info_outlined", "color": "grey"},
            "text": {"tag": "plain_text", "content": "注释", "text_size": "notation"},
        },
        {
            "tag": "img",
            "img_key": "img_v3_x",
            "alt": {"tag": "plain_text", "content": ""},
            "corner_radius": "8px",
        },
        {"tag": "person_list", "persons": [{"id": "ou_a"}], "drop_invalid_user_id": True},
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey-100",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "elements": [
                        {
                            "tag": "interactive_container",
                            "behaviors": [
                                {"type": "open_url", "default_url": "https://example.com"}
                            ],
                            "background_style": style.PANEL,
                            "border_color": "orange-200",
                            "has_border": True,
                            "corner_radius": "8px",
                            "elements": [_md()],
                        }
                    ],
                }
            ],
        },
        {
            "tag": "chart",
            "height": "180px",
            "chart_spec": {"type": "bar", "color": ["#3370EB"], "data": {"values": []}},
        },
        {
            "tag": "table",
            "columns": [{"name": "a", "data_type": "number", "format": {"precision": 2}}],
            "rows": [{"a": 1}],
            "header_style": {"background_style": "grey", "bold": True},
        },
    ]
    assert clean_elements(copy.deepcopy(elements)) == elements


def test_clean_removes_unknown_fields_recursively() -> None:
    [node] = clean_elements(
        [
            {
                "tag": "column_set",
                "fancy": True,
                "columns": [
                    {
                        "tag": "column",
                        "corner_radius": "8px",
                        "elements": [_md(extra_field=1)],
                    }
                ],
            }
        ]
    )
    assert node == {
        "tag": "column_set",
        "columns": [{"tag": "column", "elements": [_md()]}],
    }


def test_clean_drops_unknown_and_interactive_tags() -> None:
    elements = [
        {"tag": "note", "elements": []},
        {"tag": "action", "actions": []},
        {"tag": "audio", "file_key": "f"},
        {"tag": "form", "name": "f", "elements": [_md()]},
        {"tag": "input", "name": "x"},
        {"tag": "select_static", "options": []},
        {"tag": "multi_select_person", "name": "p"},
        {"tag": "date_picker", "behaviors": []},
        {"tag": "checker", "text": {"tag": "plain_text", "content": "x"}},
        "not a dict",
        _md("留下"),
    ]
    assert clean_elements(elements) == [_md("留下")]


def test_clean_behaviors_keep_only_http_open_url() -> None:
    elements = [
        {
            "tag": "interactive_container",
            "behaviors": [
                {"type": "callback", "value": {"a": 1}},
                {"type": "open_url", "default_url": "javascript:alert(1)"},
            ],
            "elements": [_md()],
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "回调"},
            "behaviors": [{"type": "callback", "value": {"a": 1}}],
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "跳转"},
            "behaviors": [
                {
                    "type": "open_url",
                    "default_url": "https://example.com",
                    "pc_url": "lark://x",
                    "bogus": 1,
                }
            ],
            "name": "b",
            "form_action_type": "submit",
        },
    ]
    box, button = clean_elements(elements)
    assert box["behaviors"] == []
    assert button == {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "跳转"},
        "behaviors": [{"type": "open_url", "default_url": "https://example.com"}],
    }


def test_clean_overflow_and_multi_url() -> None:
    [overflow, column_set] = clean_elements(
        [
            {
                "tag": "overflow",
                "options": [
                    {"text": {"tag": "plain_text", "content": "回调"}, "value": "x"},
                    {
                        "text": {"tag": "plain_text", "content": "打开"},
                        "value": "y",
                        "multi_url": {"url": "https://example.com", "ios_url": "tel://1"},
                    },
                ],
            },
            {
                "tag": "column_set",
                "action": {"multi_url": {"url": "https://example.com/a"}},
                "columns": [{"tag": "column", "action": {"multi_url": {"url": "ftp://x"}}}],
            },
        ]
    )
    assert overflow["options"] == [
        {
            "text": {"tag": "plain_text", "content": "打开"},
            "multi_url": {"url": "https://example.com"},
        }
    ]
    assert column_set["action"] == {"multi_url": {"url": "https://example.com/a"}}
    assert "action" not in column_set["columns"][0]
    assert clean_elements([{"tag": "overflow", "options": [{"value": "x"}]}]) == []


def test_clean_table_only_at_root() -> None:
    table = {"tag": "table", "columns": [{"name": "a"}], "rows": []}
    nested = [
        {"tag": "column_set", "columns": [{"tag": "column", "elements": [table, _md()]}]},
        _box(table),
        table,
    ]
    out = clean_elements(copy.deepcopy(nested))
    assert out[0]["columns"][0]["elements"] == [_md()]
    assert out[1]["elements"] == []
    assert out[2] == table
    assert clean_elements([table], root=False) == []


def test_clean_table_fields() -> None:
    [table] = clean_elements(
        [
            {
                "tag": "table",
                "columns": [
                    {"name": "a", "data_type": "sparkline", "color": "red"},
                    {"display_name": "缺 name"},
                ],
                "rows": [{"a": 1}, "bad"],
                "header_style": {"background_style": "blue", "text_color": "grey"},
            }
        ]
    )
    assert table["columns"] == [{"name": "a", "data_type": "text"}]
    assert table["rows"] == [{"a": 1}]
    assert table["header_style"] == {"text_color": "grey"}
    assert clean_elements([{"tag": "table", "columns": []}]) == []


@pytest.mark.parametrize("color", ["#FF0000", "rgba(0,0,0,0.5)", "white", "cus-unknown", 3])
def test_clean_strips_hardcoded_colors(color: Any) -> None:
    [box, div, column_set] = clean_elements(
        [
            {**_box(_md()), "background_style": color, "border_color": color},
            {"tag": "div", "text": {"tag": "plain_text", "content": "x", "text_color": color}},
            {"tag": "column_set", "background_style": color, "columns": [{"tag": "column"}]},
        ]
    )
    assert "background_style" not in box
    assert "border_color" not in box
    assert "text_color" not in div["text"]
    assert "background_style" not in column_set


def test_clean_accepts_colour_enums_and_declared_custom_colours() -> None:
    for color in ("grey", "orange-50", "grey-00", "grey-1000", "bg-white", "default", style.PANEL):
        [box] = clean_elements([{**_box(), "background_style": color}])
        assert box["background_style"] == color
    [box] = clean_elements([{**_box(), "background_style": "laser"}])
    assert box["background_style"] == "laser"
    assert {"blue", "blue-50", "sunflower-900", "grey-650"} <= COLOR_NAMES
    assert "white" not in COLOR_NAMES


def test_clean_rewrites_bad_font_colors_in_markdown() -> None:
    [node] = clean_elements(
        [_md("<font color='#ff0000'>红</font> <font color=\"green\">绿</font> <font color=red>")]
    )
    assert node["content"] == (
        "<font color='default'>红</font> <font color=\"green\">绿</font> <font color=red>"
    )


def test_clean_icons_must_be_in_library() -> None:
    [a, b, c] = clean_elements(
        [
            _md(icon={"tag": "standard_icon", "token": "chart_outlined"}),
            _md(icon={"tag": "standard_icon", "token": "time_outlined", "color": "#123456"}),
            _md(icon={"tag": "custom_icon"}),
        ]
    )
    assert "icon" not in a
    assert b["icon"] == {"tag": "standard_icon", "token": "time_outlined"}
    assert "icon" not in c


def test_clean_element_ids() -> None:
    elements = [
        _md(element_id="ok_1"),
        _md(element_id="1starts_with_digit"),
        _md(element_id="has-dash"),
        _md(element_id="x" * 21),
    ]
    out = clean_elements(elements)
    assert out[0]["element_id"] == "ok_1"
    assert all("element_id" not in n for n in out[1:])


def test_clean_required_fields() -> None:
    out = clean_elements(
        [
            {"tag": "markdown"},
            {"tag": "img", "img_key": "k"},
            {"tag": "chart", "chart_spec": "not json object"},
            {"tag": "person", "size": "small"},
            {"tag": "column_set", "columns": []},
            {"tag": "column_set", "columns": [_md()]},
            {"tag": "interactive_container"},
        ]
    )
    assert out == [
        {"tag": "img", "img_key": "k", "alt": {"tag": "plain_text", "content": ""}},
        {"tag": "interactive_container", "behaviors": [], "elements": []},
    ]


def test_clean_drops_containers_nested_deeper_than_five() -> None:
    node: dict[str, Any] = _md("最里层")
    for _ in range(6):
        node = _box(node)
    [out] = clean_elements([node])
    depth, cursor = 0, out
    while cursor.get("tag") == "interactive_container":
        depth += 1
        cursor = cursor["elements"][0] if cursor["elements"] else {}
    assert depth == 5
    assert cursor == {}


def test_clean_panel_and_collapsible() -> None:
    panel = {
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey",
        "border": {"color": "grey", "corner_radius": "8px", "width": 2},
        "header": {
            "title": {"tag": "markdown", "content": "**详情**"},
            "icon": {"tag": "standard_icon", "token": "down_outlined", "size": "16px 16px"},
            "extra": 1,
        },
        "elements": [_md()],
    }
    [out] = clean_elements([panel])
    assert out["border"] == {"color": "grey", "corner_radius": "8px"}
    assert "extra" not in out["header"]
    assert out["header"]["icon"]["size"] == "16px 16px"


def test_is_http_url() -> None:
    assert is_http_url("https://example.com/x")
    assert is_http_url("HTTP://example.com")
    for bad in ("javascript:alert(1)", "https://", "//example.com", "example.com", None, 1):
        assert not is_http_url(bad)


# ---------------------------------------------------------------- validate_card


def test_validate_accepts_minimal_card() -> None:
    assert validate_card(_card([_md()])) == []


def test_validate_card_level_fields() -> None:
    assert 'schema 应为 "2.0"' in "\n".join(validate_card({**_card([]), "schema": "1.0"}))
    card = _card([])
    card["config"]["update_multi"] = False
    assert "update_multi" in "\n".join(validate_card(card))
    card = _card([])
    card["config"]["wide_screen_mode"] = True
    assert "wide_screen_mode" in "\n".join(validate_card(card))
    assert "i18n_elements" in _problems([], i18n_elements={})
    assert "card.body 缺失" in "\n".join(validate_card({"schema": "2.0", "config": {}}))


def test_validate_element_ids() -> None:
    assert "重复" in _problems([_md(element_id="a1"), _box(_md(element_id="a1"))])
    assert "不合规" in _problems([_md(element_id="_x")])
    assert "不合规" in _problems([_md(element_id="a" * 21)])
    assert _problems([_md(element_id="a" * 20)]) == ""


def test_validate_nesting_depth() -> None:
    node: dict[str, Any] = _md()
    for _ in range(5):
        node = _box(node)
    assert _problems([node]) == ""
    assert "嵌套超过 5 层" in _problems([_box(node)])


def test_validate_table_position_and_counts() -> None:
    table = {"tag": "table", "columns": [{"name": "a"}], "rows": []}
    assert "只能放在卡片根节点" in _problems([_box(table)])
    assert _problems([dict(table) for _ in range(5)]) == ""
    assert "表格 6 个" in _problems([dict(table) for _ in range(6)])
    chart = {"tag": "chart", "chart_spec": {"type": "line"}}
    assert _problems([_box(dict(chart)) for _ in range(5)]) == ""
    assert "图表 6 个" in _problems([_box(dict(chart)) for _ in range(6)])


def test_validate_element_count_and_size() -> None:
    assert _problems([_md(str(i)) for i in range(200)]) == ""
    assert "元素 201 个" in _problems([_md(str(i)) for i in range(201)])
    assert "字节" in _problems([_md("字" * 10_500)])


def test_validate_unknown_tags_fields_icons_colors() -> None:
    assert "不支持的组件 'note'" in _problems([{"tag": "note"}])
    assert "未知字段 'fancy'" in _problems([_md(fancy=1)])
    assert "不在图标库" in _problems([_md(icon={"tag": "standard_icon", "token": "star_filled"})])
    assert "颜色" in _problems([{**_box(), "background_style": "#ffffff"}])
    assert "<font color>" in _problems([_md("<font color='rgba(1,2,3,1)'>x</font>")])
    assert "只接受 http / https" in _problems(
        [{**_box(), "behaviors": [{"type": "open_url", "default_url": "file:///etc"}]}]
    )


def test_validate_allows_callbacks_and_forms_built_by_coreman() -> None:
    button = {
        "tag": "button",
        "name": "submit",
        "form_action_type": "submit",
        "type": "primary",
        "text": {"tag": "plain_text", "content": "提交"},
        "behaviors": [{"type": "callback", "value": {"task_id": "t"}}],
    }
    form = {
        "tag": "form",
        "name": "choice_form",
        "elements": [
            {
                "tag": "multi_select_static",
                "name": "q",
                "options": [{"text": {"tag": "plain_text", "content": "A"}, "value": "a"}],
            },
            button,
        ],
    }
    assert _problems([form]) == ""
    assert "只能放在卡片根节点" in _problems([_box(form)])
    bad = {**button, "behaviors": [{"type": "callback", "value": "string"}]}
    assert "value 应为对象" in _problems([bad])


def test_validate_custom_colors_must_pair_and_differ() -> None:
    card = _card([{**_box(), "background_style": "cus-a"}])
    card["config"]["style"] = {"color": {"cus-a": {"light_mode": "rgba(0,0,0,0.1)"}}}
    assert "light_mode 和 dark_mode" in "\n".join(validate_card(card))
    card["config"]["style"] = {
        "color": {"cus-a": {"light_mode": "rgba(0,0,0,0.1)", "dark_mode": "rgba(0,0,0,0.1)"}}
    }
    problems = "\n".join(validate_card(card))
    assert "相同" in problems
    assert "cus-a" in problems
    card["config"]["style"] = {
        "color": {"cus-a": {"light_mode": "rgba(0,0,0,0.1)", "dark_mode": "rgba(255,255,255,0.1)"}}
    }
    assert validate_card(card) == []


def test_validate_custom_color_must_be_declared() -> None:
    card = _card([{**_box(), "background_style": style.PANEL}])
    assert validate_card(card) == []
    del card["config"]["style"]
    assert "颜色" in "\n".join(validate_card(card))


def test_style_custom_colors_are_paired_and_differ() -> None:
    for pair in style.CUSTOM_COLORS.values():
        assert set(pair) == {"light_mode", "dark_mode"}
        assert pair["light_mode"] != pair["dark_mode"]


def test_validate_header() -> None:
    header = {
        "title": {"tag": "plain_text", "content": "T"},
        "template": "pink",
        "text_tag_list": [
            {"tag": "text_tag", "text": {"tag": "plain_text", "content": "x"}, "color": "rgba"}
        ],
        "bogus": 1,
    }
    problems = _problems([], header=header)
    assert "template" in problems
    assert "标签颜色" in problems
    assert "未知字段 'bogus'" in problems
    assert "缺少 title" in _problems([], header={"template": "blue"})


def test_chart_spec_is_data_not_elements() -> None:
    spec = {"type": "bar", "data": [{"values": [{"tag": "x"}] * 300}], "extra": {"any": 1}}
    [chart] = clean_elements([{"tag": "chart", "chart_spec": spec}])
    assert chart["chart_spec"] == spec
    assert _problems([chart]) == ""
