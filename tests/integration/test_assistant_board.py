"""总表：按五种状态分组、停滞判断、子任务在跑算执行中、审批单单独列出、内容按密钥还原。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import board, digest, followups
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantApproval, AssistantFollowup, AssistantJob
from tests.integration.approval_helpers import install, pending, reply
from tests.integration.delivery_helpers import ready
from tests.integration.job_helpers import spawned
from tests.integration.test_assistant_digest import handover_scope

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
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
    # 助理推进的整理合同上周五起 3 个工作日没动：停滞。
    assert seqs["stalled"] == [stale.seq]
    assert [i.reason for i in result.groups["stalled"]] == ["idle"]
    # 等赵六过了期限：还在等别人，标「该催了」。
    assert seqs["others"] == [waiting.seq]
    assert [i.reason for i in result.groups["others"]] == ["nudge_due"]
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
    assert awake.idle_days == 1 and awake.stage == "working"
    [stalled] = result.groups["stalled"]
    assert stalled.row.seq == parked.seq and stalled.idle_days == 3


async def test_followups_stuck_on_a_gap(db_session: AsyncSession) -> None:
    from coreman.core.assistants import gaps

    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    stuck = await followups.add(db_session, bot.id, category="decide", title="选供应商")
    free = await followups.add(db_session, bot.id, category="promised", title="写周报")
    row, _ = await gaps.report(
        db_session,
        bot.id,
        kind="credential",
        key="QUOTE_TOKEN",
        need="查报价",
        followup_seqs=[stuck.seq],
    )
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    # 个人凭证所有者自己就能填：卡住的事在「等你」，缺口也列在那一组。
    [entry] = result.groups["owner"]
    assert entry.row.seq == stuck.seq and entry.reason == "gap"
    assert entry.gap is not None and entry.gap.id == row.id
    assert [i.row.seq for i in result.groups["working"]] == [free.seq]
    assert [(g.row.seq, g.fix, g.followups, g.stage) for g in result.gaps] == [
        (1, "credential", [stuck.seq], "owner")
    ]
    await gaps.decline(db_session, bot.id, row.seq)
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    assert result.gaps == [] and [i.row.seq for i in result.groups["owner"]] == [stuck.seq]


async def test_linked_approval_sits_under_its_followup(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    prefs = await assistant_settings.get(db_session, bot.id)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    linked = await pending(
        db_session, scope, {**reply(topic="报价"), "text": "报价已确认", "uuid": "u1"}
    )
    loose = await pending(
        db_session, scope, {**reply(topic="合同"), "text": "合同稍后发", "uuid": "u2"}
    )
    linked.followup_id = item.id
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    owner_items = result.groups["owner"]
    assert [i.row.seq for i in owner_items] == [item.seq]
    assert [a.id for a in owner_items[0].approvals] == [linked.id]
    assert [a.id for a in result.approvals] == [loose.id] and result.approvals_total == 1


async def test_gap_stuck_item_leaves_its_approval_in_the_loose_list(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.assistants import gaps

    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    prefs = await assistant_settings.get(db_session, bot.id)
    item = await followups.add(db_session, bot.id, category="promised", title="查报价")
    await gaps.report(
        db_session,
        bot.id,
        kind="credential",
        key="QUOTE_TOKEN",
        need="查报价",
        followup_seqs=[item.seq],
    )
    row = await pending(db_session, scope, {**reply(), "text": "报价已确认", "uuid": "u1"})
    row.followup_id = item.id
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    assert [(i.row.seq, i.reason) for i in result.groups["owner"]] == [(item.seq, "gap")]
    assert result.groups["owner"][0].approvals == ()
    assert [a.id for a in result.approvals] == [row.id] and result.approvals_total == 1


async def test_a_batch_sits_under_one_item_as_one_line(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    prefs = await assistant_settings.get(db_session, bot.id)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    head = await pending(db_session, scope, {**reply(), "text": "一", "uuid": "u1"})
    member = await pending(db_session, scope, {**reply(), "text": "二", "uuid": "u2"})
    head.batch_id = head.id
    member.batch_id = head.id
    member.followup_id = item.id  # 只有非代表行挂着条目：按代表行算，整张单留在单独的列表里
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    assert result.groups["owner"] == []
    assert [a.id for a in result.approvals] == [head.id]
    assert result.approvals_total == 1 and result.approval_items[head.id] == 2
    head.followup_id = item.id
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    (entry,) = result.groups["owner"]
    assert [a.id for a in entry.approvals] == [head.id]
    assert result.approval_items[head.id] == 2
    assert result.approvals == [] and result.approvals_total == 0


async def test_an_approval_linked_to_an_old_item_still_makes_the_digest_fresh(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await ready(db_session)
    scope = await handover_scope(db_session, bot, owner)
    prefs = await assistant_settings.get(db_session, bot.id)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    row = await pending(db_session, scope)
    row.followup_id = item.id
    await db_session.execute(
        update(AssistantFollowup)
        .where(AssistantFollowup.id == item.id)
        .values(updated_at=WED_NOON - timedelta(days=3))
    )
    await db_session.execute(
        update(AssistantApproval)
        .where(AssistantApproval.id == row.id)
        .values(created_at=WED_NOON + timedelta(hours=1))
    )
    prefs.digest_sent_at = WED_NOON
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON + timedelta(hours=2))
    assert result.approvals_total == 0 and result.approvals_newest is not None
    assert await digest.send_digest(db_session, bot, prefs, WED_NOON + timedelta(hours=2))


async def test_stalled_and_nudged_items(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    prefs = await assistant_settings.get(db_session, bot.id)
    lost = await followups.add(db_session, bot.id, category="promised", title="对账")
    await followups.update(db_session, bot.id, lost.seq, stuck="who")
    quiet = await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等报价",
        handler="person",
        handler_name="赵六",
    )
    for _ in range(board.MAX_NUDGES):
        await followups.progress(
            db_session, bot.id, quiet.seq, "催了一次", today=WED_NOON.date(), nudged=True
        )
    await _age(db_session, quiet, LAST_FRI)
    await db_session.commit()
    result = await board.build(db_session, bot.id, prefs, WED_NOON)
    reasons = {i.row.seq: i.reason for i in result.groups["stalled"]}
    assert reasons == {lost.seq: "stuck_who", quiet.seq: "no_reply"}
    # 换了在等的人：催办从头算，回到等别人。
    await followups.update(db_session, bot.id, quiet.seq, handler="person", handler_name="钱七")
    assert (await followups.get(db_session, bot.id, quiet.seq)).nudges == 2
    await followups.update(db_session, bot.id, quiet.seq, category="promised")
    assert (await followups.get(db_session, bot.id, quiet.seq)).nudges == 0
