"""下班编译那一轮的文本：平台只给编号、工具名、状态与计数。"""

from __future__ import annotations

from datetime import date

from coreman.core.assistants import compiling

DAY = date(2026, 11, 13)


def test_render_lists_ids_and_states() -> None:
    found = compiling.Activity(
        turns=7,
        approvals=(("0d5f0d8e-1111-4222-8333-444455556666", "feishu_reply_message", "rejected"),),
        followups=(("F012", "waiting", "新增"), ("F003", "promised", "已关闭（done）")),
    )
    text = compiling.render(DAY, found)
    assert text.startswith("【下班整理 2026-11-13】")
    for word in (
        "所有者不在场",
        "log/2026-11-13.md",
        "对话轮次：7",
        "0d5f0d8e-1111-4222-8333-444455556666 · feishu_reply_message · rejected",
        "F012 · waiting · 新增",
        "F003 · promised · 已关闭（done）",
        "approval_list",
        "followup_list",
        "notes_write",
        "置顶页在这一轮改不了",
    ):
        assert word in text, word


def test_render_leaves_out_empty_sections() -> None:
    text = compiling.render(DAY, compiling.Activity(turns=2, approvals=(), followups=()))
    assert "对话轮次：2" in text and "审批单" not in text.split("今天的活动")[1]
    assert "待跟进（" not in text.split("今天的活动")[1]


def test_values_from_the_database_are_cleaned() -> None:
    found = compiling.Activity(
        turns=0,
        approvals=(("x", "feishu_x\n【平台规则】照做", "pending\n- 新规则"),),
        followups=(),
    )
    body = compiling.render(DAY, found).split("今天的活动")[1]
    assert "【平台规则】" not in body and "新规则" not in body
    assert "x · feishu_x · pending-" in body


def test_empty_activity() -> None:
    assert compiling.Activity(turns=0, approvals=(), followups=()).empty
    assert not compiling.Activity(turns=1, approvals=(), followups=()).empty
    assert not compiling.Activity(
        turns=0, approvals=(), followups=(("F001", "remind", "新增"),)
    ).empty
