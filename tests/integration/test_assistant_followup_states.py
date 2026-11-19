"""跟进状态流转的平台侧：等别人到了催办时间交回助理（每一轮至多一次），所有者的反馈进下班编译的交代。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import compiling, delivery_checks, followups, owner_feedback, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantFollowup, AssistantInbox
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET
from tests.integration.delivery_helpers import ready
from tests.integration.shift_helpers import working
from tests.integration.test_assistant_compile import after_work, compile_jobs

pytestmark = pytest.mark.usefixtures("assistant_master")


async def _nudges(session: AsyncSession, bot_id: object) -> list[dict[str, object]]:
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot_id, AssistantInbox.kind == "reminder")
            .order_by(AssistantInbox.created_at)
        )
    )
    await sealed.open_rows(session, rows)
    return [r.summary_enc for r in rows if r.summary_enc.get("reminder") == "followup_nudge"]


async def test_waiting_items_are_handed_back_once_per_nudge_round(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    now = utcnow()
    today = assistant_settings.local_today(prefs, now)
    late = await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等报价",
        handler="person",
        handler_name="赵六",
        due_on=today - timedelta(days=1),
    )
    await followups.add(db_session, bot.id, category="waiting", title="等合同")
    await db_session.commit()
    assert await delivery_checks.nudges_due(db_session, bot.id, prefs, now) == 1
    assert await delivery_checks.nudges_due(db_session, bot.id, prefs, now) == 0
    [first] = await _nudges(db_session, bot.id)
    assert first["label"] == "F001" and first["nudges"] == 0
    assert first["handler_name"] == "赵六" and first["title"] == "等报价"
    # 催过一次：要等上次催办之后 2 个工作日才再交回。
    await followups.progress(db_session, bot.id, late.seq, "发了催办", today=today, nudged=True)
    await db_session.commit()
    assert await delivery_checks.nudges_due(db_session, bot.id, prefs, now) == 0
    await db_session.execute(
        update(AssistantFollowup)
        .where(AssistantFollowup.id == late.id)
        .values(progress_at=now - timedelta(days=10))
    )
    await db_session.commit()
    assert await delivery_checks.nudges_due(db_session, bot.id, prefs, now) == 1
    assert [n["nudges"] for n in await _nudges(db_session, bot.id)] == [0, 1]


async def test_owner_feedback_goes_into_the_compile_brief(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session)
    later = await after_work(db_session, bot, prefs)
    today = assistant_settings.local_today(prefs, utcnow())
    row = await followups.add(db_session, bot.id, category="promised", title="对账")
    other = await followups.add(db_session, bot.id, category="promised", title="订会议室")
    await owner_feedback.supplement(
        db_session, bot.id, row.seq, f"以后找财务的小王 {SECRET}", today=today
    )
    await owner_feedback.reschedule(
        db_session, bot.id, row.seq, today + timedelta(days=3), "月底再说", today=today
    )
    await owner_feedback.close(db_session, bot.id, other.seq, "not_needed", None)
    await db_session.commit()
    assert await compiling.step(db_session, bot, prefs, later) == "queued"
    await db_session.commit()
    (job,) = await compile_jobs(db_session, bot)
    lines = job.brief_enc.split("\n")
    assert compiling.FEEDBACK_HEADER in lines
    # after_work 已记了 F001。
    assert "- F002 · 补充" in lines and f"> 以后找财务的小王 {SECRET}" in lines
    assert f"- F002 · 改期 · 改到 {(today + timedelta(days=3)).isoformat()}" in lines
    assert "> 月底再说" in lines
    assert "- F003 · 关闭 · 无需处理" in lines
    assert "profile/preferences.md" in job.brief_enc


def test_feedback_is_cut_after_approvals_when_the_brief_is_too_long() -> None:
    from datetime import date

    entries = tuple(
        owner_feedback.Entry(label=f"F{i:03d}", kind="supplement", code="", note="长" * 500)
        for i in range(1, 30)
    )
    found = compiling.Activity(turns=1, approvals=(), followups=(), feedback=entries)
    text = compiling.render(date(2026, 11, 20), found)
    assert len(text) <= compiling.jobs.MAX_BRIEF
    assert "条反馈未列出" in text and "- F029 · 补充" in text
