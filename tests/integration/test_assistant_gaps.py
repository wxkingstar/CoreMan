"""缺口：按种类与标识去重并挂上待跟进；不开的不再打开；解决时刷新进展并只入一条续接提醒；怎么开按种类判定。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, gaps, sealed
from coreman.core.db.models import AssistantFollowup, AssistantInbox, BusinessSystem
from tests.integration.delivery_helpers import ready

pytestmark = pytest.mark.usefixtures("assistant_master")
OLD = datetime(2026, 11, 2, tzinfo=UTC)


async def _reminders(session: AsyncSession, bot_id) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = list(
        await session.scalars(
            select(AssistantInbox).where(
                AssistantInbox.bot_id == bot_id, AssistantInbox.kind == "reminder"
            )
        )
    )
    await sealed.open_rows(session, rows)
    return [row.summary_enc for row in rows]


async def test_report_dedupes_and_links_followups(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    first = await followups.add(db_session, bot.id, category="promised", title="查退货原因")
    second = await followups.add(db_session, bot.id, category="waiting", title="等报价")
    await db_session.execute(
        update(AssistantFollowup).where(AssistantFollowup.bot_id == bot.id).values(progress_at=OLD)
    )
    row, outcome = await gaps.report(
        db_session, bot.id, kind="business_system", key=" Orders ", need="查订单", followup_seqs=[1]
    )
    assert outcome == "created" and gaps.label(row.seq) == "G001" and row.key == "orders"
    again, outcome = await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="查订单和退款",
        followup_seqs=[2],
    )
    await db_session.commit()
    assert outcome == "updated" and again.id == row.id and again.need_enc == "查订单和退款"
    assert await gaps.stuck(db_session, bot.id, row.id) == [first.seq, second.seq]
    fresh = await followups.get(db_session, bot.id, first.seq)
    assert fresh.gap_id == row.id and fresh.progress_at > OLD


@pytest.mark.parametrize(
    ("kind", "key", "code"),
    [
        ("repo", "x", "invalid_kind"),
        ("business_system", "订单 系统", "invalid_key"),
        ("credential", "1TOKEN", "invalid_key"),
    ],
)
async def test_bad_kind_or_key(db_session: AsyncSession, kind: str, key: str, code: str) -> None:
    bot, _ = await ready(db_session)
    with pytest.raises(gaps.GapError) as caught:
        await gaps.report(db_session, bot.id, kind=kind, key=key, need="x", followup_seqs=[])
    assert caught.value.code == code


def test_credential_keys_are_env_names() -> None:
    assert gaps.normalize_key("credential", " $git_token ") == "GIT_TOKEN"
    assert gaps.normalize_key("feishu_scope", "IM:Message") == "im:message"


async def test_declined_gap_is_not_reopened(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await followups.add(db_session, bot.id, category="promised", title="发周报")
    row, _ = await gaps.report(
        db_session, bot.id, kind="cloud_product", key="slb", need="改白名单", followup_seqs=[1]
    )
    await gaps.decline(db_session, bot.id, row.seq)
    again, outcome = await gaps.report(
        db_session, bot.id, kind="cloud_product", key="slb", need="还是要改", followup_seqs=[1]
    )
    await db_session.commit()
    assert outcome == "declined" and again.status == "declined" and again.declined_at is not None
    told = await _reminders(db_session, bot.id)
    assert [t["reminder"] for t in told] == ["gap_declined"] and told[0]["followups"] == ["F001"]


async def test_resolve_refreshes_and_queues_one_reminder(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await followups.add(db_session, bot.id, category="promised", title="查库存")
    row, _ = await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查库存", followup_seqs=[1]
    )
    await db_session.execute(
        update(AssistantFollowup).where(AssistantFollowup.bot_id == bot.id).values(progress_at=OLD)
    )
    done = await gaps.resolve(db_session, bot.id, "business_system", {"ORDERS": 1, "other": 1})
    assert [g.id for g in done] == [row.id]
    assert await gaps.resolve(db_session, bot.id, "business_system", {"orders": 2}) == []
    await db_session.commit()
    assert (await gaps.get(db_session, bot.id, row.seq)).status == "resolved"
    told = await _reminders(db_session, bot.id)
    assert told == [
        {
            "reminder": "gap_resolved",
            "gap": "G001",
            "kind": "business_system",
            "key": "orders",
            "followups": ["F001"],
        }
    ]
    assert (await followups.get(db_session, bot.id, 1)).progress_at > OLD


async def test_resolved_flag_only_for_self_checked_kinds(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    with pytest.raises(gaps.GapError) as caught:
        await gaps.report(
            db_session,
            bot.id,
            kind="credential",
            key="GIT_TOKEN",
            need="x",
            followup_seqs=[],
            resolved=True,
        )
    assert caught.value.code == "resolve_not_allowed"
    row, _ = await gaps.report(
        db_session, bot.id, kind="node_tool", key="gh", need="读仓库", followup_seqs=[]
    )
    _, outcome = await gaps.report(
        db_session,
        bot.id,
        kind="node_tool",
        key="gh",
        need="读仓库",
        followup_seqs=[],
        resolved=True,
    )
    assert outcome == "resolved" and row.status == "resolved"


async def test_fix_for_business_system_follows_the_allowlist(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    db_session.add_all(
        [
            BusinessSystem(key="open_sys", name="开放的", allowed_bot_ids=None),
            BusinessSystem(key="closed_sys", name="限定的", allowed_bot_ids=[]),
        ]
    )
    await db_session.flush()
    fixes = {}
    for key in ("open_sys", "closed_sys", "missing_sys"):
        row, _ = await gaps.report(
            db_session, bot.id, kind="business_system", key=key, need="x", followup_seqs=[]
        )
        fixes[key] = await gaps.fix_for(db_session, bot.id, row)
    assert fixes == {"open_sys": "self_grant", "closed_sys": "request", "missing_sys": "admin"}


async def test_resolve_needs_a_higher_rank_than_at_report_time(db_session: AsyncSession) -> None:
    """报缺口时已经有的能力不算「开通了」：只有级别升上去才解决；重新打开时按当时的级别记。"""
    bot, _ = await ready(db_session)
    await followups.add(db_session, bot.id, category="promised", title="改订单")
    row, _ = await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="改订单",
        followup_seqs=[1],
        seen_rank=1,
    )
    assert row.seen_rank == 1
    assert await gaps.resolve(db_session, bot.id, "business_system", {"orders": 1}) == []
    assert await gaps.resolve(db_session, bot.id, "business_system", {"orders": 2}) == [row]
    # 授权又收回，再报：重新打开，按现在的级别记。
    again, outcome = await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="改订单",
        followup_seqs=[],
        seen_rank=2,
    )
    assert outcome == "updated" and again.status == "open" and again.seen_rank == 2
    # 还开着的缺口再报不改：否则级别刚升上去、还没比对就被抹平。
    await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="改订单",
        followup_seqs=[],
        seen_rank=0,
    )
    assert again.seen_rank == 2
