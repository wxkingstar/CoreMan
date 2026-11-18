"""总表的阶段与停滞推导：纯函数，不碰库。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from coreman.core.assistants import board

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(16, 23))
WEEK = [1, 2, 3, 4, 5]


def row(**kw: object) -> SimpleNamespace:
    base = {
        "category": "promised",
        "status": "open",
        "needs_owner": False,
        "handler": "assistant",
        "review_on": None,
        "due_on": None,
        "notes_enc": None,
        "progress_at": datetime(2026, 11, 16, 2, tzinfo=UTC),
    }
    return SimpleNamespace(**{**base, **kw})


def test_workdays_between_skips_weekends() -> None:
    assert board.workdays_between(FRI, MON + timedelta(days=7), WEEK) == 1
    assert board.workdays_between(MON, MON, WEEK) == 0
    assert board.workdays_between(MON, WED, WEEK) == 2
    assert board.workdays_between(FRI, SUN, WEEK) == 0
    assert board.workdays_between(FRI, SUN, [6, 7]) == 2


def test_stage_priority() -> None:
    assert board.stage(row(needs_owner=True, category="waiting"), MON) == "owner"
    assert board.stage(row(category="decide"), MON) == "owner"
    assert board.stage(row(category="waiting"), MON) == "others"
    assert board.stage(row(handler="person"), MON) == "others"
    assert board.stage(row(review_on=TUE), MON) == "scheduled"
    assert board.stage(row(status="snoozed", review_on=TUE), MON) == "scheduled"
    assert board.stage(row(review_on=MON), MON) == "working"
    assert board.stage(row(category="remind"), MON) == "working"


def test_stalled_thresholds() -> None:
    working = row()
    assert not board.is_stalled("working", working, None, 1, WED)
    assert board.is_stalled("working", working, None, 2, WED)
    running = SimpleNamespace(status="running")
    assert not board.is_stalled("working", working, running, 9, WED)
    waiting = row(category="waiting")
    assert not board.is_stalled("others", waiting, None, 4, WED)
    assert board.is_stalled("others", waiting, None, 5, WED)
    assert board.is_stalled("others", row(category="waiting", due_on=TUE), None, 0, WED)
    assert not board.is_stalled("owner", row(category="decide"), None, 30, WED)
    assert not board.is_stalled("scheduled", row(review_on=FRI), None, 30, WED)


def test_suggestion_line() -> None:
    assert (
        board.suggestion(row(notes_enc="背景\n建议：选 A，因为便宜\n[2026-11-16] x"))
        == "选 A，因为便宜"
    )
    assert board.suggestion(row(notes_enc="没有建议")) is None
    assert board.suggestion(row(notes_enc=None)) is None
    assert len(board.suggestion(row(notes_enc="建议：" + "长" * 300)) or "") == board.MAX_SUGGESTION


def test_idle_counts_from_the_review_day_once_it_has_come() -> None:
    # 推后到周二、周三醒来：从周二算起，推后的那几天不算没动。
    assert board.idle_from(FRI - timedelta(days=7), row(review_on=TUE), WED) == TUE
    # 回看日之后又有进展：取较晚的那天。
    assert board.idle_from(WED, row(review_on=MON), WED) == WED
    # 回看日还没到、或没有回看日：照最后一次进展。
    assert board.idle_from(MON, row(review_on=THU), WED) == MON
    assert board.idle_from(MON, row(), WED) == MON


def test_gap_comes_first() -> None:
    import uuid

    gap = uuid.uuid4()
    stuck = row(gap_id=gap, needs_owner=True)
    assert board.stage(stuck, MON, frozenset({gap})) == "gap"
    # 缺口已解决或所有者不开（不在 blocked 里）：回到原来的阶段。
    assert board.stage(stuck, MON, frozenset()) == "owner"
