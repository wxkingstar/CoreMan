"""顺带检查里不调飞书的两类：到期的待跟进、挂了很久的「处理中」表情；各自每天至多入箱一次。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, followups, handover, settings
from coreman.core.assistants import delivery_checks as checks
from coreman.core.db.models import AssistantMark, Bot
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import inbox_rows, ready
from tests.integration.lifecycle_helpers import CIPHER

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
TOMORROW = TODAY + timedelta(days=1)
ROUTINES = ("morning_brief", "weekly_review")


async def test_a_reminder_enters_the_box_once_per_key(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    summary = {"reminder": "mark_stale", "chat_id": "oc_li", "message_id": "om_1", "marked_at": "x"}
    added = await checks.add_reminder(
        db_session, bot.id, "mark:om_1:2026-11-13", level="batch", summary=summary, now=now
    )
    again = await checks.add_reminder(
        db_session, bot.id, "mark:om_1:2026-11-13", level="batch", summary=summary, now=now
    )
    await db_session.commit()
    assert (added, again) == (True, False)
    (row,) = await inbox_rows(db_session, bot)
    assert (row.kind, row.level, row.status) == ("reminder", "batch", "pending")
    assert row.hold_until is None and row.message_id == "mark:om_1:2026-11-13"
    assert row.summary_enc == summary


async def test_a_due_followup_is_handed_over_once_a_day(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    due = await followups.add(
        db_session, bot.id, category="promised", title=f"给李四回报价{SECRET}", due_on=TODAY
    )
    await followups.add(
        db_session, bot.id, category="waiting", title="下周再看", review_on=TOMORROW
    )
    await db_session.commit()
    assert await checks.followups_due(db_session, bot.id, TODAY, now) == 1
    assert await checks.followups_due(db_session, bot.id, TODAY, now) == 0
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert row.message_id == f"followup:{due.id}:2026-11-13"
    assert row.summary_enc == {
        "reminder": "followup_due",
        "label": "F001",
        "category": "promised",
        "due_on": "2026-11-13",
        "review_on": None,
        "title": f"给李四回报价{SECRET}",
    }
    stored = await raw(
        db_session, "SELECT summary_enc::text FROM assistant_inbox WHERE id = :id", id=row.id
    )
    assert SECRET not in stored
    # 第二天还没处理：两条都到期了，各入一次。
    assert await checks.followups_due(db_session, bot.id, TOMORROW, now) == 2
    assert await checks.followups_due(db_session, bot.id, TOMORROW, now) == 0
    # 关掉之后不再提醒。
    await followups.close(db_session, bot.id, due.seq)
    await db_session.commit()
    day_after = TOMORROW + timedelta(days=1)
    assert await checks.followups_due(db_session, bot.id, day_after, now) == 1


async def test_a_mark_left_for_four_hours_is_brought_up_once_a_day(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    old = now - timedelta(hours=4, minutes=1)
    db_session.add_all(
        [
            AssistantMark(
                bot_id=bot.id,
                message_id="om_old",
                chat_id="oc_li",
                reaction_id="r1",
                created_at=old,
            ),
            AssistantMark(
                bot_id=bot.id,
                message_id="om_new",
                chat_id="oc_li",
                reaction_id="r2",
                created_at=now - timedelta(hours=3, minutes=59),
            ),
        ]
    )
    await db_session.commit()
    assert await checks.stale_marks(db_session, bot.id, TODAY, now) == 1
    assert await checks.stale_marks(db_session, bot.id, TODAY, now) == 0
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.kind, row.level, row.chat_id) == ("reminder", "batch", "oc_li")
    assert row.message_id == "mark:om_old:2026-11-13"
    assert row.summary_enc == {
        "reminder": "mark_stale",
        "chat_id": "oc_li",
        "message_id": "om_old",
        "marked_at": old.isoformat(),
    }
    # 第二天还挂着：再提一次；撤掉之后不提。
    assert await checks.stale_marks(db_session, bot.id, TOMORROW, now) == 1
    await db_session.execute(delete(AssistantMark).where(AssistantMark.message_id == "om_old"))
    await db_session.commit()
    assert await checks.stale_marks(db_session, bot.id, TOMORROW + timedelta(days=1), now) == 0


async def test_reminders_ride_along_with_the_next_handover(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await followups.add(db_session, bot.id, category="remind", title="续签合同", due_on=TODAY)
    await db_session.commit()
    await checks.followups_due(db_session, bot.id, TODAY, now)
    await db_session.commit()
    task = await delivery.dispatch(db_session, bot, now)
    await db_session.commit()
    assert task is not None
    text = handover.render(await delivery.bound(db_session, task), delivery.zone_named(None))
    assert "【1】待跟进到期 · 攒批" in text and "待跟进 F001（remind）今天到期" in text
    assert "> 续签合同" in text


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """所有者（默认上海时区）2026-11 月 `day` 日的当地时刻，换成 UTC。11-13 是周五。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC) - timedelta(hours=8)


async def routine_kinds(session: AsyncSession, bot: Bot) -> list[str]:
    return [
        row.summary_enc["reminder"]
        for row in await inbox_rows(session, bot)
        if row.kind == "reminder" and row.summary_enc["reminder"] in ROUTINES
    ]


async def test_morning_brief_is_added_once_from_half_an_hour_before_the_first_digest(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.quiet_enabled = False  # 开着免打扰时，它结束的那一刻也算一个摘要时点
    assert await checks.routines(db_session, bot.id, prefs, local(13, 8, 29)) == 0
    assert await routine_kinds(db_session, bot) == []
    assert await checks.routines(db_session, bot.id, prefs, local(13, 8, 30)) == 1
    assert await checks.routines(db_session, bot.id, prefs, local(13, 8, 35)) == 0
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.kind, row.level, row.summary_enc) == (
        "reminder",
        "batch",
        {"reminder": "morning_brief"},
    )
    # 周五 08:30 不是周五最后一个摘要之前的 60 分钟，每周的还没到；次日（周六）每日的照常生成。
    assert await checks.routines(db_session, bot.id, prefs, local(14, 8, 30)) == 1
    assert await routine_kinds(db_session, bot) == ["morning_brief", "morning_brief"]


async def test_weekly_review_is_added_on_the_last_workday_before_the_last_digest(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.quiet_enabled = False  # 开着免打扰时，它结束的那一刻也算一个摘要时点
    # 周四 17:00：不是最后一个工作日。
    await checks.routines(db_session, bot.id, prefs, local(12, 17))
    assert "weekly_review" not in await routine_kinds(db_session, bot)
    await checks.routines(db_session, bot.id, prefs, local(13, 16, 59))
    assert "weekly_review" not in await routine_kinds(db_session, bot)
    assert await checks.routines(db_session, bot.id, prefs, local(13, 17)) == 1
    assert await checks.routines(db_session, bot.id, prefs, local(13, 17, 30)) == 0
    assert (await routine_kinds(db_session, bot)).count("weekly_review") == 1
    await db_session.commit()
    rows = [
        r for r in await inbox_rows(db_session, bot) if r.summary_enc["reminder"] == "weekly_review"
    ]
    assert [(r.level, r.message_id) for r in rows] == [("batch", "weekly_review:2026-W46")]
    # 工作日改成周一到周三：周三就是最后一个工作日。
    prefs.workdays = [1, 2, 3]
    await db_session.commit()
    await checks.routines(db_session, bot.id, prefs, local(18, 17))
    assert (await routine_kinds(db_session, bot)).count("weekly_review") == 2


async def test_routines_wait_until_cold_start_is_done(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.quiet_enabled = False  # 开着免打扰时，它结束的那一刻也算一个摘要时点
    prefs.cold_start_done_at = None
    assert await checks.routines(db_session, bot.id, prefs, local(13, 17)) == 0
    assert await inbox_rows(db_session, bot) == []


@pytest.mark.usefixtures("runtime_settings")
async def test_sweep_counts_routines(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.quiet_enabled = False  # 开着免打扰时，它结束的那一刻也算一个摘要时点
    row = await delivery.state(db_session, bot.id)
    assert row is not None
    counts = await checks.sweep(db_session, CIPHER, bot, prefs, row, local(13, 17))
    assert counts["assistant_routines"] == 2


def test_each_routine_renders_a_title_and_one_platform_line() -> None:
    for kind in ROUTINES:
        lines = handover._reminder({"reminder": kind})
        assert len(lines) == 1 and "交回事项" in lines[0]
        assert handover.REMINDERS[kind]
