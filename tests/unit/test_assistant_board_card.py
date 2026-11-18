"""总表卡片的拼装：放不下的只说个数，卡片不超过飞书的大小；空总表说一句；谁在办的说法。不碰库。"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any

from coreman.core.assistants import board, digest
from coreman.core.db.models import AssistantApproval, AssistantFollowup, AssistantJob
from coreman.core.feishu_cards.compile import MAX_CARD_BYTES

TODAY = date(2026, 11, 18)
HINT = "发「看板」或在「我的助理」页看全部"


def _row(seq: int, **kw: Any) -> AssistantFollowup:
    base: dict[str, Any] = {
        "seq": seq,
        "category": "promised",
        "handler": "assistant",
        "title_enc": f"{seq:03d}" + "长" * 197,
        "notes_enc": "建议：" + "议" * 100,
    }
    return AssistantFollowup(**{**base, **kw})


def _board(per_group: int = 10) -> board.Board:
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    stages = {"owner": "owner", "stalled": "working", "working": "working", "others": "others"}
    seq = 0
    for name in board.GROUPS:
        for _ in range(per_group):
            seq += 1
            if name == "scheduled":
                row = _row(seq, review_on=TODAY + timedelta(days=2))
                item = board.Item(row, "scheduled", False, 0, None)
            else:
                row = _row(seq, category="decide" if name == "owner" else "promised")
                item = board.Item(row, stages[name], name == "stalled", 3, None)
            groups[name].append(item)
    approvals = [
        AssistantApproval(
            id=uuid.uuid4(), tool="feishu_im_send_message", args_enc={"abstract": "批" * 200}
        )
        for _ in range(board.APPROVALS_MAX)
    ]
    return board.Board(TODAY, groups, approvals, 15, {}, None)


def _size(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode())


def _texts(card: dict[str, Any]) -> str:
    out: list[str] = []
    for node in card["body"]["elements"]:
        if node.get("tag") == "div":
            out.append(node["text"]["content"])
        elif node.get("tag") == "markdown":
            out.append(node["content"])
    return "\n".join(out)


def _card(result: board.Board, *, full: bool) -> tuple[dict[str, Any], list[int]]:
    reports = [f"{i:02d}" + "做" * 298 for i in range(digest.REPORTS_MAX)]
    return digest.board_card(
        result,
        requester="owner_pid",
        link="https://tower.example.com",
        system=None,
        full=full,
        reports=None if full else reports,
    )


def test_a_crowded_board_stays_within_the_card_limit_and_says_what_was_cut() -> None:
    result = _board()
    for full in (True, False):
        card, buttons = _card(result, full=full)
        assert _size(card) < MAX_CARD_BYTES
        text = _texts(card)
        assert HINT in text
        # 卡片上的按钮与登记的编号一致：没放下的条目不留按钮。
        found = [n["element_id"] for n in card["body"]["elements"] if n.get("tag") == "column_set"]
        assert found == [f"fu{seq}" for seq in buttons] and buttons
        # 条目照顺序放到放不下为止。
        assert "001" in text


def test_a_small_board_lists_everything_without_hints() -> None:
    result = _board(per_group=1)
    result.approvals[:] = result.approvals[:1]
    result = board.Board(TODAY, result.groups, result.approvals, 1, {}, None)
    card, buttons = _card(result, full=True)
    text = _texts(card)
    assert HINT not in text and "另有" not in text
    assert buttons == [1]
    for seq in range(1, 6):
        assert f"{seq:03d}" in text


def test_an_empty_board_says_there_is_nothing_to_follow_up() -> None:
    empty = board.Board(TODAY, {name: [] for name in board.GROUPS}, [], 0, {}, None)
    card, buttons = _card(empty, full=True)
    assert digest.EMPTY_BOARD in _texts(card) and buttons == []
    digest_card, _ = digest.board_card(
        empty, requester="owner_pid", link=None, system=None, full=False, reports=["发了周报"]
    )
    assert digest.EMPTY_BOARD not in _texts(digest_card)


def test_who_is_on_it() -> None:
    def who(row: AssistantFollowup, job: AssistantJob | None = None) -> str:
        return digest._who(board.Item(row, "working", False, 0, job))

    assert who(_row(1, handler="job"), AssistantJob(kind="bot_help")) == "公共员工在办"
    assert who(_row(1, handler="job"), AssistantJob(kind="subtask")) == "子任务在办"
    assert who(_row(1, handler="job")) == "助理在办"
    assert who(_row(1, handler="person", handler_name_enc="赵六")) == "赵六 在办"
    assert who(_row(1, handler="person")) == "同事在办"
    assert who(_row(1, handler="owner")) == "你在办"
