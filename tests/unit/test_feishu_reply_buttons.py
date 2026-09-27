"""回复按钮：value 的生成与解析、点过之后的置灰，以及 people 块要解析的人名。"""

import copy
import json
from typing import Any

import pytest

from coreman.core.feishu_cards import components as c
from coreman.core.feishu_cards.compile import compile_reply, people_names, scan
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.reply_buttons import (
    USED_TIPS,
    UsedRow,
    batch_actions,
    mark_card,
    parse_reply,
    reply_value,
)
from coreman.core.feishu_cards.sanitize import validate_card
from coreman.core.richtext.schema import ActionsBlock, BlockError, RawBlock, validate_block

ACTIONS = (
    '先说结论。\n\n```card:actions\n{"buttons": ['
    '{"text": "打开", "url": "https://example.com/a"}, '
    '{"text": "继续分析", "reply": true}, '
    '{"text": "按品类统计", "reply": "隐藏的另一句话"}]}\n```'
)


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in _walk(card) if n.get("tag") == "button"]


def test_reply_is_only_a_marker() -> None:
    for marker in (True, "true", "任何一句话", ""):
        [button] = ActionsBlock.model_validate([{"text": "好", "reply": marker}]).buttons
        assert button.is_reply, marker
    with pytest.raises(BlockError):
        validate_block("actions", [{"text": "好", "reply": False}])
    with pytest.raises(BlockError):
        validate_block("actions", [{"text": "好", "reply": True, "url": "https://example.com"}])
    with pytest.raises(BlockError):
        validate_block("actions", [{"text": "长" * 41, "reply": True}])


def test_value_roundtrip() -> None:
    value = reply_value("继续", requester="u_1", row="cols_1", buttons=["btn_1"])
    assert value == {
        "action": "reply",
        "text": "继续",
        "requester": "u_1",
        "row": "cols_1",
        "buttons": ["btn_1"],
    }
    click = parse_reply(value)
    assert click is not None
    assert (click.text, click.requester, click.row, click.buttons) == (
        "继续",
        "u_1",
        "cols_1",
        ("btn_1",),
    )


def test_malformed_values_are_ignored() -> None:
    good = reply_value("继续", requester="u_1", row="cols_1", buttons=["btn_1"])
    assert parse_reply("reply") is None
    assert parse_reply({**good, "action": "other"}) is None
    for text in ("", "  ", ["x"], "长" * 41):
        assert parse_reply({**good, "text": text}) is None, text
    assert parse_reply({**good, "text": "长" * 40}) is not None
    # 没有提问人（或提问人 ID 可疑）的不是我们渲染的。
    for requester in (None, "", "u' x", "u" * 65):
        assert parse_reply({**good, "requester": requester}) is None, requester
    # 定位用的 ID 不合规就不做置灰，消息照发。
    odd = parse_reply({**good, "row": "1bad", "buttons": ["ok_1", "bad id", 3]})
    assert odd is not None and odd.row is None and odd.buttons == ("ok_1",)


def test_raw_blocks_cannot_forge_reply_callbacks() -> None:
    """回调 value 里写着提问人：只有渲染器能生成，raw 块里的回调一律被裁掉。"""
    forged = RawBlock(
        elements=[
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "x"},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": reply_value("x", requester="u", row="r", buttons=[]),
                    }
                ],
            }
        ]
    )
    rendered = c.render_raw(forged, RenderContext(requester="u_1"))
    assert "callback" not in json.dumps(rendered)


def test_compiled_reply_buttons_only_with_a_requester() -> None:
    plain = compile_reply(ACTIONS)
    assert [b["text"]["content"] for b in _buttons(plain.cards[0])] == ["打开"]
    assert "「继续分析」" in json.dumps(plain.cards[0], ensure_ascii=False)
    compiled = compile_reply(ACTIONS, requester="u_1")
    assert compiled.problems == []
    assert validate_card(compiled.cards[0]) == []
    buttons = _buttons(compiled.cards[0])
    values = [b["behaviors"][0].get("value") for b in buttons]
    assert values[0] is None
    assert [v["text"] for v in values[1:]] == ["继续分析", "按品类统计"]
    assert {v["requester"] for v in values[1:]} == {"u_1"}
    assert values[1]["buttons"] == [buttons[1]["element_id"], buttons[2]["element_id"]]
    # 发出去的只可能是看得见的文字。
    assert "隐藏的另一句话" not in json.dumps(compiled.cards, ensure_ascii=False)


def _used(card: dict[str, Any], index: int, chat: str = "group") -> UsedRow:
    click = parse_reply(_buttons(card)[index]["behaviors"][0]["value"])
    assert click is not None and click.row is not None
    used = UsedRow.parse(
        {"row": click.row, "buttons": list(click.buttons), "label": click.text, "chat_type": chat}
    )
    assert used is not None
    return used


def test_used_row_roundtrip_and_validation() -> None:
    used = UsedRow("cols_1", ("btn_1",), "继续", "single")
    assert UsedRow.parse(used.dump()) == used
    assert UsedRow.parse({**used.dump(), "chat_type": "p2p"}).chat_type == "group"  # type: ignore[union-attr]
    for bad in ({"row": "1x"}, {"label": ""}, {"label": "长" * 41}, {"row": None}):
        assert UsedRow.parse({**used.dump(), **bad}) is None, bad
    assert UsedRow.parse("x") is None


def test_batch_actions_disable_reply_buttons_and_note_who_chose() -> None:
    card = compile_reply(ACTIONS, requester="u_1").cards[0]
    used = _used(card, 1)
    actions = batch_actions(used, "u_1")
    patched = [
        a["params"]["element_id"] for a in actions if a["action"] == "partial_update_element"
    ]
    assert patched == list(used.buttons)
    assert actions[0]["params"]["partial_element"] == {
        "disabled": True,
        "disabled_tips": {"tag": "plain_text", "content": USED_TIPS},
    }
    [add] = [a for a in actions if a["action"] == "add_elements"]
    assert add["params"]["type"] == "insert_after"
    assert add["params"]["target_element_id"] == used.row
    [note] = add["params"]["elements"]
    assert note["element_id"] == f"{used.row}_used"
    # 群里写明是谁点的：人员标签只展示，不发通知。
    assert note["content"] == (
        "<person id='u_1' show_avatar=false></person> <font color='grey'>已选择：继续分析</font>"
    )
    # 私聊里只有本人，不写名字；可疑的 ID 不拼进标记。
    single = batch_actions(_used(card, 1, "single"), "u_1")[-1]["params"]["elements"][0]
    assert single["content"] == "<font color='grey'>已选择：继续分析</font>"
    odd = batch_actions(used, "u' onclick='x")[-1]["params"]["elements"][0]
    assert "person" not in odd["content"]


def test_mark_card_updates_json_card_once() -> None:
    card = compile_reply(ACTIONS, requester="u_1").cards[0]
    used = _used(card, 2)
    marked = copy.deepcopy(card)
    assert mark_card(marked, used, "u_1")
    buttons = _buttons(marked)
    assert "disabled" not in buttons[0]  # 链接按钮点过回复之后照样能用
    assert all(b["disabled"] is True for b in buttons[1:])
    notes = [n for n in _walk(marked) if n.get("element_id") == f"{used.row}_used"]
    assert len(notes) == 1 and "按品类统计" in notes[0]["content"]
    assert validate_card(marked) == []
    again = copy.deepcopy(marked)
    assert mark_card(again, used, "u_2")
    assert again == marked
    # 这张卡里没有这一行（别的消息、旧卡）：不动。
    other = copy.deepcopy(card)
    stale = UsedRow("cols_99", used.buttons, used.label, "group")
    assert not mark_card(other, stale, "u_1")
    assert other == card


def test_people_names_cover_blocks_columns_and_person_columns() -> None:
    text = (
        '```card:people\n{"users": ["alice@example.com", "bob", "alice@example.com"]}\n```\n\n'
        '```card:columns\n{"columns": [[{"type": "people", "users": ["carol"]}], '
        '[{"type": "note", "text": "口径"}]]}\n```\n\n'
        '```card:table\n{"columns": [{"key": "who", "type": "person"}, {"key": "n"}], '
        '"rows": [{"who": "dave", "n": "erin"}, {"who": ["frank", "bob"]}, {"who": 3}]}\n```\n\n'
        "普通文字里提到 grace 不算。![图](https://example.com/a.png)"
    )
    assert people_names(text) == ["alice@example.com", "bob", "carol", "dave", "frank"]
    assert people_names("没有块") == []
    assert scan(text) == (["https://example.com/a.png"], people_names(text))
