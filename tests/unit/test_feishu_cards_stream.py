"""流式增量布局：逐字回放样例回复，每一步都按规划的操作改一张「模拟卡片」，检查一致性。"""

import json
from typing import Any

import pytest

from coreman.core.feishu_cards.samples import SAMPLES
from coreman.core.feishu_cards.stream import STREAM_BUDGET, plan, stream_units

INITIAL = [{"kind": "text", "ids": ["answer"], "text": "…", "digest": ""}]


class FakeCard:
    """只关心根层级组件的顺序和正文内容，按飞书的 delete / insert_after 语义执行。"""

    def __init__(self) -> None:
        self.ids = ["thinking_panel", "answer"]
        self.content: dict[str, str] = {"answer": "…"}

    def apply(self, actions: list[dict[str, Any]], texts: list[tuple[str, str]]) -> None:
        for action in actions:
            params = action["params"]
            if action["action"] == "delete_elements":
                for eid in params["element_ids"]:
                    assert eid in self.ids, f"删除不存在的组件 {eid}"
                    self.ids.remove(eid)
            else:
                assert params["type"] == "insert_after"
                target = params["target_element_id"]
                assert target in self.ids, f"插入目标不存在 {target}"
                at = self.ids.index(target) + 1
                new = [e["element_id"] for e in params["elements"]]
                assert not set(new) & set(self.ids), f"组件 id 重复 {new}"
                self.ids[at:at] = new
                for e in params["elements"]:
                    if e["tag"] == "markdown":
                        self.content[e["element_id"]] = e["content"]
        for eid, content in texts:
            assert eid in self.ids, f"更新不存在的正文 {eid}"
            self.content[eid] = content


def replay(text: str, step: int = 7) -> tuple[FakeCard, list[dict[str, Any]], int, int]:
    card, layout = FakeCard(), list(INITIAL)
    typed = rewrites = 0
    for end in [*range(step, len(text), step), len(text)]:
        steps = plan(layout, stream_units(text[:end]), anchor="thinking_panel", shown=2_000)
        for eid, content in steps.texts:
            old = card.content.get(eid, "")
            if content.startswith(old):
                typed += 1
            else:
                rewrites += 1
        card.apply(steps.actions, steps.texts)
        layout = steps.layout
        # 卡片上的根组件 = 思考面板 + 布局里记的所有单元，顺序一致。
        assert card.ids == ["thinking_panel", *[i for r in layout for i in r["ids"]]]
    return card, layout, typed, rewrites


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_replaying_every_sample_keeps_card_and_layout_in_sync(name: str) -> None:
    _card, layout, typed, rewrites = replay(SAMPLES[name])
    # 打字机效果要求旧文本是新文本的前缀：绝大多数更新都应满足。
    assert typed > 0 and rewrites <= max(1, typed // 5)
    final = stream_units(SAMPLES[name])
    assert [r["ids"] for r in layout] == [list(u.ids) for u in final]


def test_blocks_appear_when_finished_and_placeholder_is_replaced() -> None:
    text = SAMPLES["item"]
    cut = text.index('"highlight"')
    units = stream_units(text[:cut])
    shown = json.dumps([list(u.elements) for u in units], ensure_ascii=False)
    assert "正在" in shown or "⏳" in shown
    done = stream_units(text)
    kinds = [u.kind for u in done]
    assert kinds.count("block") >= 3
    assert "正在" not in json.dumps([list(u.elements) for u in done], ensure_ascii=False)


def test_header_is_skipped_while_streaming() -> None:
    units = stream_units(SAMPLES["daily"][:60])
    assert units == []
    units = stream_units(SAMPLES["daily"])
    assert all("经营日报 · 9 月 25 日" not in json.dumps(list(u.elements)) for u in units)


def test_two_half_charts_become_one_row_when_the_second_finishes() -> None:
    text = SAMPLES["daily"]
    second = text.index("```card:chart", text.index("```card:chart") + 1)
    before = stream_units(text[:second])
    after = stream_units(text[: text.index("💡")])
    row = [u for u in after if u.kind == "block" and "column_set" in json.dumps(list(u.elements))]
    assert len(row) >= 1
    assert sum(json.dumps(list(u.elements)).count('"chart"') for u in after) >= 2
    assert len(after) <= len(before) + 1


def test_budget_freezes_growth_instead_of_overflowing() -> None:
    note = '```card:note\n{"text": "口径"}\n```'
    long = "很长的一段说明。" * 1500 + f"\n\n{note}\n\n" + "另一段很长的说明。" * 1500
    card = FakeCard()
    steps = plan(INITIAL, stream_units(long), anchor="thinking_panel", shown=2_000)
    card.apply(steps.actions, steps.texts)
    assert steps.frozen
    shown = sum(len(json.dumps(c, ensure_ascii=False).encode()) for c in card.content.values())
    assert shown + 2_000 <= STREAM_BUDGET


def test_clipped_text_is_not_resent() -> None:
    long = "很长的一段说明。" * 2000
    first = plan(INITIAL, stream_units(long), anchor="thinking_panel", shown=2_000)
    again = plan(
        first.layout, stream_units(long + "再多一点"), anchor="thinking_panel", shown=2_000
    )
    assert first.texts and again.texts == []


def test_unchanged_text_produces_no_calls() -> None:
    units = stream_units("同样的内容")
    first = plan(INITIAL, units, anchor="thinking_panel", shown=0)
    again = plan(first.layout, stream_units("同样的内容"), anchor="thinking_panel", shown=0)
    assert again.actions == [] and again.texts == []


def test_show_is_applied_to_displayed_text_but_layout_keeps_original() -> None:
    steps = plan(
        INITIAL,
        stream_units("原文"),
        anchor="thinking_panel",
        shown=0,
        show=lambda t: t + "!",
    )
    assert steps.texts == [("answer", "原文!")]
    assert steps.layout[0]["text"] == "原文"
