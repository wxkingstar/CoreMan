"""第二批卡片块：引用来源、可折叠段落、表单，以及相邻图片合成多图。"""

import json
from typing import Any

import pytest

from coreman.core.feishu_cards.compile import compile_reply
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.markdown import render_markdown
from coreman.core.feishu_cards.reply_buttons import FORM_LABEL, UsedRow, parse_form
from coreman.core.feishu_cards.sanitize import validate_card
from coreman.core.richtext.degrade import to_markdown
from coreman.core.richtext.schema import BlockError, validate_block


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _tags(card: dict[str, Any]) -> list[str]:
    return [n["tag"] for n in _walk(card) if "tag" in n]


SOURCES = (
    "结论在这。\n\n```card:sources\n"
    '[{"title": "官方文档", "url": "https://example.com/doc", "note": "第 3 节"},'
    ' {"title": "内部记录", "url": "ftp://x"}]\n```'
)


def test_sources_list_links_with_hosts() -> None:
    compiled = compile_reply(SOURCES)
    assert compiled.problems == []
    text = json.dumps(compiled.cards[0], ensure_ascii=False)
    assert "1. [官方文档](https://example.com/doc) <font color='grey'>example.com</font>" in text
    assert "2. 内部记录" in text and "ftp://x" not in text
    assert "来源：\n1. [官方文档](https://example.com/doc)" in to_markdown(SOURCES)


def test_details_is_a_collapsed_panel_without_tables() -> None:
    reply = (
        '```card:details\n{"title": "完整日志", "text": "| a | b |\\n|---|---|\\n| 1 | 2 |\\n\\n'
        '```\\nline\\n```"}\n```'
    )
    compiled = compile_reply(reply)
    assert compiled.problems == []
    [panel] = [n for n in _walk(compiled.cards[0]) if n.get("tag") == "collapsible_panel"]
    assert panel["expanded"] is False and "完整日志" in panel["header"]["title"]["content"]
    assert "table" not in _tags(panel)
    assert "**完整日志**" in to_markdown(reply)


FORM = (
    '```card:form\n{"title": "出差申请", "fields": ['
    '{"name": "city", "label": "城市", "required": true},'
    '{"name": "day", "label": "日期", "type": "date"},'
    '{"name": "kind", "label": "类型", "type": "select", "options": ["高铁", "飞机"]},'
    '{"name": "note", "label": "备注", "type": "textarea"}]}\n```'
)


def test_form_renders_only_for_the_requester_and_submits_as_a_message() -> None:
    plain = compile_reply(FORM)
    assert "form" not in _tags(plain.cards[0])
    assert "请提供「出差申请」" in json.dumps(plain.cards, ensure_ascii=False)
    compiled = compile_reply(FORM, requester="u_1")
    assert compiled.problems == [] and validate_card(compiled.cards[0]) == []
    [form] = [n for n in _walk(compiled.cards[0]) if n.get("tag") == "form"]
    assert form in compiled.cards[0]["body"]["elements"]  # 表单只能在根节点
    [submit] = [n for n in _walk(form) if n.get("form_action_type") == "submit"]
    value = submit["behaviors"][0]["value"]
    assert value["labels"] == {"city": "城市", "day": "日期", "kind": "类型", "note": "备注"}
    assert value["row"] == form["element_id"] and value["buttons"][-1] == submit["element_id"]
    click = parse_form(
        value, {"city": "上海", "day": "2026-11-20 +0800", "kind": "高铁", "extra": "伪造"}
    )
    assert click is not None and click.requester == "u_1" and click.label == FORM_LABEL
    assert click.text == (
        "（填写了表单「出差申请」）\n城市：上海\n日期：2026-11-20 +0800\n类型：高铁\n备注：（未填）"
    )
    used = UsedRow.parse(
        {
            "row": click.row,
            "buttons": list(click.buttons),
            "label": FORM_LABEL,
            "chat_type": "single",
        }
    )
    assert used is not None and len(used.buttons) == 5


def test_form_values_are_checked() -> None:
    assert parse_form({"action": "reply"}, {}) is None
    assert parse_form({"action": "form", "requester": "bad id", "labels": {"a": "A"}}, {}) is None
    assert parse_form({"action": "form", "requester": "u_1", "labels": {"1x": "A"}}, {}) is None
    with pytest.raises(BlockError):
        validate_block("form", {"fields": [{"name": "a", "label": "A", "type": "select"}]})
    with pytest.raises(BlockError):
        validate_block("form", {"fields": [{"name": "a", "label": "A", "options": ["x"]}]})
    with pytest.raises(BlockError):
        validate_block(
            "form", {"fields": [{"name": "a", "label": "A"}, {"name": "a", "label": "B"}]}
        )


def test_adjacent_image_paragraphs_become_one_gallery() -> None:
    ctx = RenderContext()
    md = "看图：\n\n![a](img_v3_a)\n\n![b](img_v3_b)\n\n![c](img_v3_c)\n\n说明\n\n![d](img_v3_d)"
    out = render_markdown(md, ctx)
    tags = [e["tag"] for e in out]
    assert tags == ["markdown", "img_combination", "markdown", "img"]
    assert [i["img_key"] for i in out[1]["img_list"]] == ["img_v3_a", "img_v3_b", "img_v3_c"]
    assert out[1]["combination_mode"] == "triple"


WIDE = (
    "| 订单 | 金额 | 状态 | 渠道 |\n|---|--:|---|---|\n"
    "| A1 | 12 | 已发货 | 官网 |\n| A2 | 30 | 待付款 |  |"
)


def test_mobile_turns_wide_tables_into_records_and_stacks_half_blocks() -> None:
    desktop = compile_reply(WIDE)
    assert "table" in _tags(desktop.cards[0])
    phone = compile_reply(WIDE, mobile=True)
    text = json.dumps(phone.cards[0], ensure_ascii=False)
    assert "table" not in _tags(phone.cards[0])
    assert "**A1**\\n金额：12 · 状态：已发货 · 渠道：官网" in text
    assert "**A2**\\n金额：30 · 状态：待付款" in text
    narrow = compile_reply(
        "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n| 7 | 8 |\n| 9 | 0 |\n| x | y |",
        mobile=True,
    )
    assert "records" not in json.dumps(narrow.cards, ensure_ascii=False)
    block = (
        '```card:table\n{"columns": [{"key": "a", "title": "名"}, {"key": "b"}, {"key": "c"}, '
        '{"key": "d"}], "rows": [{"a": "x", "b": 1, "c": 2, "d": 3}]}\n```'
    )
    assert "**x**\\nb：1 · c：2 · d：3" in json.dumps(
        compile_reply(block, mobile=True).cards[0], ensure_ascii=False
    )
    halves = (
        '```card:callout\n{"level": "info", "text": "甲", "size": "half"}\n```\n\n'
        '```card:callout\n{"level": "info", "text": "乙", "size": "half"}\n```'
    )
    assert "column_set" in _tags(compile_reply(halves).cards[0])
    assert "column_set" not in _tags(compile_reply(halves, mobile=True).cards[0])


def test_client_kind_from_user_agent() -> None:
    from coreman.runtime.gateway_feishu.inbound import client_kind

    assert client_kind(None) is None and client_kind("") is None
    assert client_kind("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_2_1) Lark/6.7.5") == "desktop"
    assert (
        client_kind("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Lark/7.30") == "mobile"
    )
    assert client_kind("Mozilla/5.0 (Linux; Android 14) Feishu/7.30") == "mobile"
