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
        "stuck": None,
        "nudges": 0,
        "progress_at": datetime(2026, 11, 16, 2, tzinfo=UTC),
    }
    return SimpleNamespace(**{**base, **kw})


def test_workdays_between_skips_weekends() -> None:
    assert board.workdays_between(FRI, MON + timedelta(days=7), WEEK) == 1
    assert board.workdays_between(MON, MON, WEEK) == 0
    assert board.workdays_between(MON, WED, WEEK) == 2
    assert board.workdays_between(FRI, SUN, WEEK) == 0
    assert board.workdays_between(FRI, SUN, [6, 7]) == 2


def stage(r: SimpleNamespace, today: date = MON, **kw: object) -> tuple[str, str]:
    return board.classify(r, today, **kw)  # type: ignore[arg-type]


def test_owner_states() -> None:
    assert stage(row(needs_owner=True, category="waiting")) == ("owner", "info")
    assert stage(row(category="decide")) == ("owner", "decide")
    assert stage(row(category="remind")) == ("owner", "remind")
    assert stage(row(category="remind", review_on=MON)) == ("owner", "remind")
    assert stage(row(handler="owner")) == ("owner", "self")
    assert stage(row(), awaiting=True) == ("owner", "approval")
    assert stage(row(), job=SimpleNamespace(status="blocked")) == ("owner", "job_blocked")


def test_working_means_something_is_moving() -> None:
    assert stage(row()) == ("working", "assistant")
    assert stage(row(), idle=1) == ("working", "assistant")
    assert stage(row(), job=SimpleNamespace(status="running"), idle=9) == ("working", "job_running")
    assert stage(row(), job=SimpleNamespace(status="queued")) == ("working", "job_queued")
    # 子任务做完了：回到助理自己推进，照进展判断。
    assert stage(row(), job=SimpleNamespace(status="done")) == ("working", "assistant")
    assert stage(row(review_on=MON)) == ("working", "assistant")


def test_stalled_reasons() -> None:
    assert stage(row(), idle=2) == ("stalled", "idle")
    assert stage(row(stuck="how")) == ("stalled", "stuck_how")
    assert stage(row(stuck="who", category="waiting")) == ("stalled", "stuck_who")
    assert stage(row(), job=SimpleNamespace(status="failed")) == ("stalled", "job_failed")
    # 要所有者的事不算停滞。
    assert stage(row(category="decide", stuck="how"), idle=30) == ("owner", "decide")


def test_scheduled_needs_a_start_day() -> None:
    assert stage(row(review_on=TUE)) == ("scheduled", "review")
    assert stage(row(category="remind", review_on=TUE)) == ("scheduled", "review")
    # 所有者改了期：哪怕在等他或在等别人，也按排期列。
    snoozed = row(status="snoozed", review_on=TUE)
    assert stage(snoozed) == ("scheduled", "snoozed")
    assert stage(row(status="snoozed", review_on=TUE, category="decide")) == (
        "scheduled",
        "snoozed",
    )
    assert stage(row(status="snoozed", review_on=TUE, category="waiting")) == (
        "scheduled",
        "snoozed",
    )


def test_waiting_and_nudges() -> None:
    waiting = row(category="waiting")
    assert stage(waiting) == ("others", "waiting")
    assert stage(row(handler="person")) == ("others", "waiting")
    assert stage(waiting, idle=1) == ("others", "waiting")
    assert stage(waiting, idle=board.NUDGE_AFTER) == ("others", "nudge_due")
    # 过了期限就该催，不用等进展的天数。
    assert stage(row(category="waiting", due_on=SUN - timedelta(days=7)), WED) == (
        "others",
        "nudge_due",
    )
    # 催过之后只看上次催办以来的天数。
    nudged = row(category="waiting", due_on=MON, nudges=1)
    assert stage(nudged, WED, idle=1) == ("others", "waiting")
    assert stage(nudged, WED, idle=2) == ("others", "nudge_due")
    assert stage(row(category="waiting", nudges=board.MAX_NUDGES), idle=2) == (
        "stalled",
        "no_reply",
    )


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


def test_need_line() -> None:
    assert board.need(row(notes_enc="背景\n需要你：报价单和截止日\n")) == "报价单和截止日"
    assert board.need(row(notes_enc="没有")) is None


def test_gap_comes_first() -> None:
    stuck = row(needs_owner=True)
    assert stage(stuck, gap="owner") == ("owner", "gap")
    assert stage(stuck, gap="others") == ("others", "gap_requested")
    assert stage(stuck, gap="stalled") == ("stalled", "gap_admin")
    # 缺口已解决或所有者不开：回到原来的状态。
    assert stage(stuck) == ("owner", "info")


def test_gap_stage_follows_how_to_fix() -> None:
    open_ = SimpleNamespace(status="open")
    for fix in ("authorize", "credential", "self_grant", "request"):
        assert board.gap_stage(open_, fix) == "owner"  # type: ignore[arg-type]
    for fix in ("admin", "node_admin", "none"):
        assert board.gap_stage(open_, fix) == "stalled"  # type: ignore[arg-type]
    assert board.gap_stage(SimpleNamespace(status="requested"), "request") == "others"  # type: ignore[arg-type]
