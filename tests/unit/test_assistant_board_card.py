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
    reasons = {"owner": "decide", "stalled": "idle", "working": "assistant", "others": "waiting"}
    seq = 0
    for name in board.GROUPS:
        for _ in range(per_group):
            seq += 1
            if name == "scheduled":
                row = _row(seq, review_on=TODAY + timedelta(days=2))
                item = board.Item(row, "scheduled", "review", 0, None)
            else:
                row = _row(seq, category="decide" if name == "owner" else "promised")
                item = board.Item(row, name, reasons[name], 3, None)
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
        return digest._who(board.Item(row, "working", "assistant", 0, job))

    assert who(_row(1, handler="job"), AssistantJob(kind="bot_help")) == "公共员工在办"
    assert who(_row(1, handler="job"), AssistantJob(kind="subtask")) == "子任务在办"
    assert who(_row(1, handler="job")) == "助理在办"
    assert who(_row(1, handler="person", handler_name_enc="赵六")) == "赵六 在办"
    assert who(_row(1, handler="person")) == "同事在办"
    assert who(_row(1, handler="owner")) == "你在办"


def _ids(node: Any) -> list[str]:
    """卡片里所有的 element_id，按出现顺序。"""
    out: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("element_id"), str):
            out.append(node["element_id"])
        for value in node.values():
            out.extend(_ids(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_ids(value))
    return out


def test_gap_group_has_hint_and_buttons() -> None:
    from coreman.core.assistants import gaps
    from coreman.core.db.models import AssistantGap

    chat = AssistantGap(
        seq=3, kind="feishu_scope", key="im:message", need_enc="读群消息", status="open"
    )
    admin = AssistantGap(seq=4, kind="cloud_product", key="slb", need_enc="改白名单", status="open")
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    groups["owner"] = [board.Item(_row(1), "owner", "gap", 0, None, chat)]
    result = board.Board(
        TODAY,
        groups,
        [],
        0,
        {},
        None,
        [board.Gap(chat, "authorize", [1], "owner"), board.Gap(admin, "admin", [], "stalled")],
    )
    card, _ = digest.board_card(result, requester="ou_x", link=None, system=None, full=True)
    text = json.dumps(card, ensure_ascii=False)
    # 所有者自己能开的列在「等你」，要找管理员的列在「停滞」。
    assert "要你开通的权限（1）" in text and "停滞（0）" in text
    assert text.index("G003") < text.index("停滞（0）") < text.index("G004")
    assert "要你开通权限" in text
    assert "G003 缺飞书权限 im:message：读群消息 → 卡住 F001" in text
    assert gaps.hint(chat, "authorize") in text and gaps.hint(admin, "admin") in text
    assert "开通 G003" in text and "不开 G003" in text
    assert "开通 G004" not in text and "不开 G004" in text
    # 按钮行与按钮的元素编号：网关凭它们置灰；回调里只有所有者能点。
    ids = _ids(card)
    assert ["gp3", "go3", "gd3", "gp4", "gd4"] == [i for i in ids if i[:2] in ("gp", "go", "gd")]
    clicks = [
        b["behaviors"][0]["value"]
        for b in _buttons(card)
        if b["element_id"] in ("go3", "gd3", "gd4")
    ]
    assert [(v["text"], v["row"], v["buttons"], v["requester"]) for v in clicks] == [
        ("开通 G003", "gp3", ["go3", "gd3"], "ou_x"),
        ("不开 G003", "gp3", ["go3", "gd3"], "ou_x"),
        ("不开 G004", "gp4", ["gd4"], "ou_x"),
    ]
    # 摘要（非看板）也展开等你与停滞：两个缺口都在。
    brief, _ = digest.board_card(result, requester="ou_x", link=None, system=None, full=False)
    brief_text = json.dumps(brief, ensure_ascii=False)
    assert "G003" in brief_text and "G004" in brief_text


def _buttons(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("tag") == "button":
            out.append(node)
        for value in node.values():
            out.extend(_buttons(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_buttons(value))
    return out


def test_many_gaps_stay_within_the_card_limit() -> None:
    from coreman.core.db.models import AssistantGap

    entries = [
        board.Gap(
            AssistantGap(
                seq=n, kind="credential", key=f"TOKEN_{n}", need_enc="要" * 200, status="open"
            ),
            "credential",
            list(range(1, 30)),
            "owner",
        )
        for n in range(1, 40)
    ]
    # 只有缺口：列满 `LIST_MAX` 个，余下的在本组末尾说个数。
    alone: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    result = board.Board(TODAY, alone, [], 0, {}, None, entries)
    card, _ = digest.board_card(result, requester="ou_x", link=None, system=None, full=True)
    texts = _texts(card)
    assert _size(card) <= MAX_CARD_BYTES
    assert texts.count("缺个人凭证") == digest.LIST_MAX
    assert texts.endswith(digest.MORE_HINT.format(n=len(entries) - digest.LIST_MAX))
    # 前面几组已经把卡片占满：缺口一个也放不下，照样说个数，卡片不超限。
    full = _board()
    result = board.Board(
        TODAY, full.groups, full.approvals, full.approvals_total, {}, None, entries
    )
    card, _ = digest.board_card(result, requester="ou_x", link=None, system=None, full=True)
    texts = _texts(card)
    assert _size(card) <= MAX_CARD_BYTES
    shown = texts.count("缺个人凭证")
    tail = texts.split("要你开通的权限（39）", 1)[1].split("\n**", 1)[0]
    assert digest.MORE_HINT.format(n=len(entries) - shown) in tail


def test_linked_approval_line_under_item() -> None:
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    approval = AssistantApproval(
        id=uuid.uuid4(), tool="feishu_im_send_message", args_enc={"abstract": "私聊 → 李四"}
    )
    groups["owner"] = [board.Item(_row(1), "owner", "approval", 0, None, None, (approval,))]
    card, _ = digest.board_card(
        board.Board(TODAY, groups, [], 0, {}, None),
        requester="ou_x",
        link="https://tower.example.com",
        system=None,
        full=True,
    )
    text = json.dumps(card, ensure_ascii=False)
    assert "待批：私聊 → 李四" in text and "https://tower.example.com" in text


def test_batch_under_item_is_one_line_with_its_item_count() -> None:
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    approval = AssistantApproval(
        id=uuid.uuid4(), tool="feishu_im_send_message", args_enc={"abstract": "私聊 → 李四"}
    )
    groups["owner"] = [board.Item(_row(1), "owner", "approval", 0, None, None, (approval,))]
    card, _ = digest.board_card(
        board.Board(TODAY, groups, [], 0, {approval.id: 2}, None),
        requester="ou_x",
        link=None,
        system=None,
        full=True,
    )
    text = json.dumps(card, ensure_ascii=False)
    assert text.count("待批：") == 1 and "共 2 项" in text


def test_requested_gap_has_no_open_button() -> None:
    """已申请、在等管理员的缺口：卡片上不再给「开通」，只留「不开」。"""
    from coreman.core.assistants import gaps
    from coreman.core.db.models import AssistantGap

    waiting = AssistantGap(
        seq=5, kind="business_system", key="orders", need_enc="查订单", status="requested"
    )
    fresh = AssistantGap(seq=6, kind="business_system", key="crm", need_enc="查客户", status="open")
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    result = board.Board(
        TODAY,
        groups,
        [],
        0,
        {},
        None,
        [board.Gap(waiting, "request", [], "others"), board.Gap(fresh, "request", [], "owner")],
    )
    card, _ = digest.board_card(result, requester="ou_x", link=None, system=None, full=True)
    text = json.dumps(card, ensure_ascii=False)
    assert (
        gaps.hint(waiting, "request") == "已申请，等平台管理员处理"
        and "已申请，等平台管理员处理" in text
    )
    assert "开通 G005" not in text and "不开 G005" in text
    assert "开通 G006" in text and "不开 G006" in text


def test_lines_say_why_and_when() -> None:
    def line(stage: str, reason: str, **kw: Any) -> str:
        return digest._line(board.Item(_row(7, **kw), stage, reason, 2, None))

    assert "不知道找谁" in line("stalled", "stuck_who") and "2 个工作日没动" in line(
        "stalled", "idle"
    )
    assert "该催了" in line("others", "nudge_due")
    assert "在等对方" not in line("others", "waiting")
    assert f"{TODAY + timedelta(days=3)} 启动" in line(
        "scheduled", "review", review_on=TODAY + timedelta(days=3)
    )


def test_owner_item_shows_what_is_needed() -> None:
    groups: dict[str, list[board.Item]] = {name: [] for name in board.GROUPS}
    row = _row(1, notes_enc="需要你：给出报价上限")
    groups["owner"] = [board.Item(row, "owner", "info", 0, None)]
    card, _ = digest.board_card(
        board.Board(TODAY, groups, [], 0, {}, None),
        requester="ou_x",
        link=None,
        system=None,
        full=False,
    )
    assert "需要你：给出报价上限" in _texts(card)
