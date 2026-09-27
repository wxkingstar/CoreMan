import json
from typing import Any

import pytest

from coreman.core.feishu_cards.compile import (
    MAX_CARD_BYTES,
    _chunks,
    compile_reply,
    count_elements,
    image_urls,
    simple_cards,
    streaming_text,
    summary_of,
)
from coreman.core.feishu_cards.samples import SAMPLE_IMAGE_URL, SAMPLES
from coreman.core.feishu_cards.sanitize import validate_card
from coreman.core.feishu_cards.thinking import thinking_panel
from coreman.core.richtext.degrade import readable


def tags(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [node["tag"]] if "tag" in node else []
        return own + [t for v in node.values() for t in tags(v)]
    if isinstance(node, list):
        return [t for v in node for t in tags(v)]
    return []


def size(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode())


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_sample_compiles_to_valid_cards(name: str) -> None:
    compiled = compile_reply(
        SAMPLES[name], prefix=[thinking_panel("步骤")], images={SAMPLE_IMAGE_URL: "img_v3_x"}
    )
    assert compiled.problems == []
    assert compiled.rich
    assert compiled.summary
    for card in compiled.cards:
        assert validate_card(card) == []
        assert size(card) <= MAX_CARD_BYTES
    # 思考面板只在主卡最前面。
    assert compiled.cards[0]["body"]["elements"][0]["tag"] == "collapsible_panel"


def test_header_block_becomes_card_header_and_summary() -> None:
    compiled = compile_reply(SAMPLES["daily"])
    card = compiled.cards[0]
    assert card["header"]["title"]["content"] == "经营日报 · 9 月 25 日"
    assert card["header"]["template"] == "turquoise"
    assert card["config"]["summary"] == {"content": "经营日报 · 9 月 25 日"}
    assert card["config"]["width_mode"] == "fill"
    assert "header" not in tags(card["body"])


def test_adjacent_half_blocks_share_one_row() -> None:
    compiled = compile_reply(SAMPLES["daily"])
    rows = [
        e
        for e in compiled.cards[0]["body"]["elements"]
        if e["tag"] == "column_set" and tags(e).count("chart") == 2
    ]
    assert len(rows) == 1
    assert rows[0]["flex_mode"] == "stretch"


def test_lone_half_block_is_rendered_full_width() -> None:
    text = '```card:chart\n{"chart": "pie", "size": "half", "items": [["a", 1], ["b", 2]]}\n```'
    body = compile_reply(text).cards[0]["body"]["elements"]
    # 落单的半宽图表占整行，仍放在面板里。
    assert len(body) == 1 and tags(body[0]).count("chart") == 1
    assert body[0]["tag"] == "interactive_container"


def test_sixth_chart_degrades_to_a_data_table() -> None:
    block = '```card:chart\n{"chart": "bar", "x": ["a", "b"], "series": [{"values": [1, 2]}]}\n```'
    compiled = compile_reply("\n\n".join([block] * 6))
    card = compiled.cards[0]
    assert tags(card).count("chart") == 5
    # 第 6 个降级成数据表（数字列触发转成 table 组件），数据不丢。
    assert tags(card).count("table") == 1
    assert compiled.problems == []


def test_long_reply_spills_into_continuation_cards() -> None:
    paragraph = "这是一段很长的说明文字。" * 200
    text = "\n\n".join(f"## 第 {i} 节\n\n{paragraph}" for i in range(20))
    compiled = compile_reply(text, prefix=[thinking_panel("步骤")])
    assert len(compiled.cards) > 1
    assert compiled.problems == []
    for index, card in enumerate(compiled.cards):
        assert size(card) <= MAX_CARD_BYTES
        assert count_elements(card) <= 200
        first = card["body"]["elements"][0]["tag"]
        assert (first == "collapsible_panel") == (index == 0)
        assert ("summary" in card["config"]) == (index == 0)
    joined = json.dumps(compiled.cards, ensure_ascii=False)
    assert joined.count("第 19 节") == 1


def test_streaming_shows_placeholder_for_unfinished_block() -> None:
    text = '结论先行。\n\n```card:chart\n{"chart": "line", "x": ["a"'
    compiled = compile_reply(text, final=False)
    content = json.dumps(compiled.cards[0], ensure_ascii=False)
    assert "正在生成图表" in content
    assert '"x"' not in content
    assert "正在生成图表" in streaming_text(text)
    assert streaming_text(text).startswith("结论先行。")


def test_invalid_block_is_shown_as_code_not_dropped() -> None:
    text = '```card:kpi\n{"items": "not a list of objects"}\n```'
    content = json.dumps(compile_reply(text).cards[0], ensure_ascii=False)
    assert "```json" in content
    assert "not a list of objects" in content


def test_streaming_text_degrades_finished_blocks_and_keeps_plain_text() -> None:
    assert streaming_text("普通回答，没有块。") == "普通回答，没有块。"
    text = SAMPLES["logistics"]
    shown = streaming_text(text)
    assert "```card" not in shown
    assert "鉴定通过" in shown


def test_simple_cards_have_only_markdown_and_never_truncate() -> None:
    [card] = simple_cards(SAMPLES["inspection"], prefix=[thinking_panel("x")])
    assert set(tags(card["body"]["elements"][1:])) == {"markdown"}
    assert validate_card(card) == []
    body = "".join(f"第{i}段。" * 30 + "\n\n" for i in range(2000))
    cards = simple_cards(body)
    assert len(cards) > 1
    assert all(size(c) <= MAX_CARD_BYTES and validate_card(c) == [] for c in cards)
    assert "第1999段" in json.dumps(cards[-1], ensure_ascii=False)


def test_simple_cards_budget_counts_json_escapes() -> None:
    code = "```json\n" + '{"k": "v\\n"}\n' * 3000 + "```"
    cards = simple_cards(code)
    assert all(size(c) <= MAX_CARD_BYTES for c in cards)


def test_long_code_block_is_split_on_lines_with_fences_repaired() -> None:
    code = "\n\n".join(f"# comment {i}\nx = {i}  # " + "注释" * 40 for i in range(200))
    chunks = _chunks(f"```python\n{code}\n```\n\n结尾。", 3000)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    assert all(c.startswith("```python") for c in chunks[:-1])


@pytest.mark.parametrize(
    "text",
    [
        '```card:table\n{"columns": [{"key": "a"}], "rows": [{"a": "x\\ud83d"}]}\n```',
        '```card:table\n{"columns": [{"key": "a", "type": "number"}], "rows": [{"a": '
        + "9" * 400
        + "}]}\n```",
        '```card:chart\n{"chart": "radar", "x": ["a", "b", "c"], '
        '"series": [{"values": [1e308, 1, 2]}]}\n```',
        '```card:raw\n{"elements": [{"tag": "markdown", "content": "hi", "margin": NaN}]}\n```',
        '```card:kpi\n{"items": [{"label": "x", "value": Infinity}]}\n```',
    ],
)
def test_hostile_block_input_never_breaks_delivery(text: str) -> None:
    compiled = compile_reply(text)
    assert compiled.problems == []
    for card in compiled.cards:
        json.dumps(card, allow_nan=False).encode("utf-8")
    streaming_text(text).encode("utf-8")
    readable(text).encode("utf-8")
    simple_cards(text)


def test_renderer_crash_degrades_only_that_block(monkeypatch: pytest.MonkeyPatch) -> None:
    import coreman.core.feishu_cards.compile as compile_module

    def boom(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(compile_module, "render_kpi", boom)
    compiled = compile_reply(SAMPLES["item"])
    assert compiled.problems == []
    content = json.dumps(compiled.cards, ensure_ascii=False)
    assert "浏览" in content and "建议调到 $4,099" in content


def test_oversized_table_is_split_by_rows_not_dropped() -> None:
    wide = " | ".join(["很长的单元格内容" * 3] * 6)
    rows = "\n".join(f"| 行{i} | {wide} |" for i in range(300))
    head = "| 名称 | " + " | ".join(f"列{i}" for i in range(6)) + " |\n|" + "---|" * 7
    compiled = compile_reply(f"{head}\n{rows}\n\n结论在最后。")
    assert compiled.problems == []
    assert len(compiled.cards) > 1
    joined = json.dumps(compiled.cards, ensure_ascii=False)
    assert "行299" in joined and "结论在最后" in joined


def test_every_card_has_its_own_fallback_text() -> None:
    paragraph = "说明文字。" * 400
    text = "\n\n".join(f"## 第 {i} 节\n\n{paragraph}" for i in range(20))
    compiled = compile_reply(text)
    assert len(compiled.texts) == len(compiled.cards) > 1
    assert "第 0 节" in compiled.texts[0] and "第 19 节" in compiled.texts[-1]
    assert sum(t.count("## 第") for t in compiled.texts) == 20


def test_unclosed_block_at_the_end_keeps_following_prose() -> None:
    text = '前文。\n\n```card:note\n{"text": "口径说明"}\n后面忘了关围栏的正文。'
    content = json.dumps(compile_reply(text).cards, ensure_ascii=False)
    assert "口径说明" in content and "后面忘了关围栏的正文" in content
    assert "```json" not in content


def test_image_urls_include_item_and_column_images() -> None:
    text = (
        "![a](https://example.com/a.png)\n\n"
        '```card:item\n{"title": "t", "image": "https://example.com/b.png"}\n```\n\n'
        '```card:columns\n{"columns": [[{"type": "item", "title": "c", '
        '"image": "https://example.com/c.png"}], ["![d](https://example.com/d.png)"]]}\n```'
    )
    assert image_urls(text) == [
        "https://example.com/a.png",
        "https://example.com/b.png",
        "https://example.com/c.png",
        "https://example.com/d.png",
    ]


def test_summary_is_first_plain_sentence() -> None:
    assert summary_of("**GMV** 涨了 [12%](https://example.com)\n\n详情") == "GMV 涨了 12%"
    assert summary_of("") == ""


def test_empty_reply_still_produces_a_valid_card() -> None:
    compiled = compile_reply("")
    assert compiled.problems == []
    assert compiled.cards[0]["body"]["elements"]
