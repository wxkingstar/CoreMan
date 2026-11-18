"""总表：按阶段分组、停滞判断、子任务在跑不算停滞、审批单单独列出、内容按密钥还原。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import board, followups
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantFollowup, AssistantJob
from tests.integration.approval_helpers import install, pending
from tests.integration.delivery_helpers import ready
from tests.integration.job_helpers import spawned
from tests.integration.test_assistant_digest import handover_scope

pytestmark = pytest.mark.usefixtures("assistant_master")
WED_NOON = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)  # 北京周三 12:00
LAST_FRI = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)


async def _age(session: AsyncSession, row: AssistantFollowup, at: datetime) -> None:
    await session.execute(
        update(AssistantFollowup).where(AssistantFollowup.id == row.id).values(progress_at=at)
    )


async def test_groups_and_stalls(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    decide = await followups.add(
        db_session, bot.id, category="decide", title="选供应商", notes="建议：选 A"
    )
    fresh = await followups.add(db_session, bot.id, category="promised", title="写周报")
    stale = await followups.add(db_session, bot.id, category="promised", title="整理合同")
    waiting = await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等赵六回复",
        due_on=WED_NOON.date() - timedelta(days=1),
    )
    later = await followups.add(
        db_session,
        bot.id,
        category="remind",
        title="续约",
        review_on=WED_NOON.date() + timedelta(days=3),
    )
    job = await spawned(db_session, bot)
    running = await followups.add(
        db_session, bot.id, category="promised", title="查退货", handler="job", job_id=job.id
    )
    for row in (stale, running):
        await _age(db_session, row, LAST_FRI)
    await db_session.execute(
        update(AssistantJob)
        .where(AssistantJob.id == job.id)
        .values(status="running", updated_at=LAST_FRI)
    )
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    seqs = {name: [i.row.seq for i in items] for name, items in result.groups.items()}
    assert seqs["owner"] == [decide.seq]
    # 停滞组按没动的工作日数从多到少：整理合同（上周五起 3 个工作日）在前，过期的等赵六在后。
    assert seqs["stalled"] == [stale.seq, waiting.seq]
    assert set(seqs["working"]) == {fresh.seq, running.seq}
    # 子任务还在跑：即使很久没动，也不算停滞。
    assert running.seq not in seqs["stalled"]
    assert seqs["scheduled"] == [later.seq]
    assert result.counts()["owner"] == 1
    assert board.suggestion(result.groups["owner"][0].row) == "选 A"


async def test_pending_approvals_are_listed_separately(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    await pending(db_session, scope)
    prefs = await assistant_settings.get(db_session, bot.id)
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    assert result.approvals_total == 1 and len(result.approvals) == 1
    # 审批单不算进待跟进的任何一组。
    assert sum(result.counts().values()) == 0


async def test_a_woken_item_does_not_count_its_snoozed_days_as_idle(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    woken = await followups.add(db_session, bot.id, category="promised", title="整理合同")
    parked = await followups.add(db_session, bot.id, category="promised", title="对账")
    # 上周五起就没动；一条推后到昨天（周二），今天（周三）醒来，另一条一直开着。
    await db_session.execute(
        update(AssistantFollowup)
        .where(AssistantFollowup.id.in_([woken.id, parked.id]))
        .values(progress_at=LAST_FRI)
    )
    await db_session.execute(
        update(AssistantFollowup)
        .where(AssistantFollowup.id == woken.id)
        .values(status="snoozed", review_on=WED_NOON.date() - timedelta(days=1))
    )
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    [awake] = [i for i in result.groups["working"] if i.row.seq == woken.seq]
    assert awake.idle_days == 1 and not awake.stalled
    [stalled] = result.groups["stalled"]
    assert stalled.row.seq == parked.seq and stalled.idle_days == 3
