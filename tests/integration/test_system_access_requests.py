"""申请：只给白名单外的系统建单；同一系统只有一张在途；拒绝让缺口回到 open 并告诉助理；
批准把助理加进白名单。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_requests as ar
from coreman.core.assistants import followups, gaps, sealed
from coreman.core.db.models import (
    AssistantInbox,
    BusinessSystem,
    OutboxItem,
    PlatformApp,
    User,
    UserIdentity,
)
from tests.integration.delivery_helpers import ready

pytestmark = pytest.mark.usefixtures("assistant_master")


async def _setup(session: AsyncSession, allowed: list[uuid.UUID] | None):  # type: ignore[no-untyped-def]
    """`allowed` 原样写进系统的白名单：None 是对所有机器人开放，[] 是谁都不开放。"""
    bot, owner = await ready(session)
    session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=allowed))
    manager = User(login_name="admin1", display_name="王五", role="platform_admin")
    app = PlatformApp(
        platform="feishu", name="通知", capabilities=["notify"], app_id="cli_notify", secret_enc="x"
    )
    session.add_all([manager, app])
    await session.flush()
    session.add(UserIdentity(user_id=manager.id, platform="feishu", platform_user_id="wangwu"))
    await followups.add(session, bot.id, category="promised", title="查订单")
    await gaps.report(
        session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[1]
    )
    await session.commit()
    return bot, owner, manager


async def _reminders(session: AsyncSession, bot_id: uuid.UUID) -> list[dict]:  # type: ignore[type-arg]
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot_id, AssistantInbox.kind == "reminder")
            .order_by(AssistantInbox.message_at, AssistantInbox.id)
        )
    )
    await sealed.open_rows(session, rows)
    return [row.summary_enc for row in rows]


async def test_open_notify_and_reject(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    assert (
        await gaps.fix_for(db_session, bot.id, await gaps.get(db_session, bot.id, 1)) == "request"
    )
    row = await ar.open_request(
        db_session, bot, 1, write=False, reason="查客户订单状态", actor=owner
    )
    await db_session.commit()
    gap = await gaps.get(db_session, bot.id, 1)
    assert row.status == "pending" and gap.status == "requested"
    assert gaps.hint(gap, "request") == "已申请，等平台管理员处理"
    notes = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.kind == "notify").order_by(OutboxItem.id)
        )
    )
    assert [n.target["user_id"] for n in notes] == [str(manager.id)]
    assert (
        "订单系统" in notes[0].payload["content"]
        and "查客户订单状态" in notes[0].payload["content"]
    )
    # 缺口已经是「已申请」：再提交一次被拦在缺口状态上。
    with pytest.raises(ar.RequestError) as caught:
        await ar.open_request(db_session, bot, 1, write=False, reason="再来", actor=owner)
    assert caught.value.code == "gap_not_open"
    row_id = row.id
    await ar.reject(db_session, row_id, actor=manager, note="先找业务负责人确认")
    await db_session.commit()
    gap = await gaps.get(db_session, bot.id, 1)
    assert gap.status == "open"
    told = await _reminders(db_session, bot.id)
    assert [t["reminder"] for t in told] == ["gap_rejected"]
    assert told[0]["note"] == "先找业务负责人确认" and told[0]["followups"] == ["F001"]
    with pytest.raises(ar.RequestError) as caught:
        await ar.reject(db_session, row_id, actor=manager, note="再拒一次")
    assert caught.value.code == "request_decided"


async def test_one_pending_request_per_system(db_session: AsyncSession) -> None:
    """缺口被改回 open（例如别处改了状态）时，同一系统已有在途申请照样拦下。"""
    bot, owner, _ = await _setup(db_session, allowed=[])
    await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    gap = await gaps.get(db_session, bot.id, 1)
    gap.status = "open"
    await db_session.commit()
    with pytest.raises(ar.RequestError) as caught:
        await ar.open_request(db_session, bot, 1, write=True, reason="还要写", actor=owner)
    assert caught.value.code == "already_pending"


async def test_open_systems_are_granted_by_the_owner_not_requested(
    db_session: AsyncSession,
) -> None:
    bot, owner, _ = await _setup(db_session, allowed=None)
    assert (
        await gaps.fix_for(db_session, bot.id, await gaps.get(db_session, bot.id, 1))
        == "self_grant"
    )
    with pytest.raises(ar.RequestError) as caught:
        await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    assert caught.value.code == "owner_can_grant"


async def test_unrequestable_systems_and_bad_reasons(db_session: AsyncSession) -> None:
    bot, owner, _ = await _setup(db_session, allowed=[])
    for reason in ("   ", "长" * (ar.MAX_REASON + 1)):
        with pytest.raises(ar.RequestError) as caught:
            await ar.open_request(db_session, bot, 1, write=False, reason=reason, actor=owner)
        assert caught.value.code == "invalid_reason"
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None
    system.enabled = False
    await db_session.commit()
    with pytest.raises(ar.RequestError) as caught:
        await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    assert caught.value.code == "system_not_requestable"
    await db_session.rollback()
    await db_session.refresh(bot)
    await gaps.report(
        db_session, bot.id, kind="cloud_product", key="slb", need="改白名单", followup_seqs=[]
    )
    await db_session.commit()
    with pytest.raises(ar.RequestError) as caught:
        await ar.open_request(db_session, bot, 2, write=False, reason="改白名单", actor=owner)
    assert caught.value.code == "not_business_system"


async def test_mark_approved_adds_the_bot_to_the_allowlist(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=True, reason="改订单备注", actor=owner)
    await db_session.commit()
    before = (await db_session.get(BusinessSystem, "orders")).version  # type: ignore[union-attr]
    done, system, added = await ar.mark_approved(db_session, row.id, actor=manager)
    await db_session.commit()
    assert added and done.status == "approved" and done.decided_by == manager.id
    assert system.allowed_bot_ids == [bot.id] and system.version == before + 1
    with pytest.raises(ar.RequestError) as caught:
        await ar.mark_approved(db_session, row.id, actor=manager)
    assert caught.value.code == "request_decided"
    with pytest.raises(ar.RequestError) as caught:
        await ar.mark_approved(db_session, uuid.uuid4(), actor=manager)
    assert caught.value.code == "request_not_found"


async def test_mark_approved_when_already_allowed_adds_nothing(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None
    system.allowed_bot_ids = [bot.id]
    await db_session.commit()
    done, system, added = await ar.mark_approved(db_session, row.id, actor=manager)
    assert not added and done.status == "approved" and system.allowed_bot_ids == [bot.id]


async def test_withdraw(db_session: AsyncSession) -> None:
    bot, owner, _ = await _setup(db_session, allowed=[])
    await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    row = await ar.withdraw(db_session, bot.id, 1)
    await db_session.commit()
    assert row.status == "withdrawn" and (await gaps.get(db_session, bot.id, 1)).status == "open"
    with pytest.raises(ar.RequestError) as caught:
        await ar.withdraw(db_session, bot.id, 1)
    assert caught.value.code == "request_not_found"
    with pytest.raises(gaps.GapError) as missing:
        await ar.withdraw(db_session, bot.id, 9)
    assert missing.value.code == "gap_not_found"


async def test_latest_for_and_view(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    first = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await ar.withdraw(db_session, bot.id, 1)
    second = await ar.open_request(db_session, bot, 1, write=True, reason="改订单", actor=owner)
    await db_session.commit()
    gap = await gaps.get(db_session, bot.id, 1)
    latest = await ar.latest_for(db_session, [gap.id])
    assert first.id != second.id and latest[gap.id].id == second.id
    assert await ar.latest_for(db_session, []) == {}
    shown = ar.view(second, bot_name="助理", system_name="订单系统", requester="张三")
    assert shown["status"] == "pending" and shown["write"] is True and shown["reason"] == "改订单"
    assert shown["decided_at"] is None and shown["system_name"] == "订单系统"


async def test_open_note_for_a_requested_gap_has_no_steps(db_session: AsyncSession) -> None:
    """已申请的缺口：所有者再点「开通」只告诉他在等管理员，不再给提交申请的步骤。"""
    bot, owner, _ = await _setup(db_session, allowed=[])
    note = await gaps.open_note(db_session, bot.id, "开通 G001")
    assert note is not None and "gap_request" in note
    await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await db_session.commit()
    note = await gaps.open_note(db_session, bot.id, "开通 G001")
    assert note is not None and "等平台管理员处理" in note
    assert "gap_request" not in note and "「申请」" not in note


async def test_withdraw_then_decide_returns_clean_codes(db_session: AsyncSession) -> None:
    """撤回与审批先后落在同一张申请上：后到的一方拿到清楚的错误码，不改状态。"""
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await db_session.commit()
    row_id = row.id
    await ar.withdraw(db_session, bot.id, 1)
    await db_session.commit()
    for decide in (
        ar.reject(db_session, row_id, actor=manager, note="不批"),
        ar.mark_approved(db_session, row_id, actor=manager),
    ):
        with pytest.raises(ar.RequestError) as caught:
            await decide
        assert caught.value.code == "request_decided"
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == []
    assert await _reminders(db_session, bot.id) == []


async def test_reject_then_withdraw(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await db_session.commit()
    await ar.reject(db_session, row.id, actor=manager, note="不批")
    await db_session.commit()
    with pytest.raises(ar.RequestError) as caught:
        await ar.withdraw(db_session, bot.id, 1)
    assert caught.value.code == "request_not_found"


async def test_approving_for_a_disabled_bot_is_stale(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    bot.enabled = False
    await db_session.commit()
    row_id = row.id
    with pytest.raises(ar.RequestError) as caught:
        await ar.mark_approved(db_session, row_id, actor=manager)
    assert caught.value.code == "request_stale"
    await db_session.commit()
    done = await db_session.get(ar.SystemAccessRequest, row_id, populate_existing=True)
    assert done is not None and done.status == "withdrawn"
    assert done.decided_by == manager.id and done.decision_note == ar.STALE_BOT
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == []


async def test_approving_when_the_gap_moved_on_is_stale(db_session: AsyncSession) -> None:
    """缺口已经不在「已申请」（这里直接改状态模拟）：批准不动白名单，申请记撤回。"""
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    gap = await gaps.get(db_session, bot.id, 1)
    gap.status = "resolved"
    await db_session.commit()
    row_id = row.id
    with pytest.raises(ar.RequestError) as caught:
        await ar.mark_approved(db_session, row_id, actor=manager)
    assert caught.value.code == "request_stale"
    await db_session.commit()
    done = await db_session.get(ar.SystemAccessRequest, row_id, populate_existing=True)
    assert done is not None and done.status == "withdrawn" and done.decision_note == ar.STALE_GAP
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == []


async def test_declining_a_requested_gap_withdraws_the_request(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await db_session.commit()
    row_id = row.id
    await gaps.decline(db_session, bot.id, 1)
    await db_session.commit()
    done = await db_session.get(ar.SystemAccessRequest, row_id, populate_existing=True)
    assert done is not None and done.status == "withdrawn" and done.decided_by is None
    assert (await gaps.get(db_session, bot.id, 1)).status == "declined"
    with pytest.raises(ar.RequestError) as caught:
        await ar.mark_approved(db_session, row_id, actor=manager)
    assert caught.value.code == "request_decided"
    assert [t["reminder"] for t in await _reminders(db_session, bot.id)] == ["gap_declined"]


async def test_rejecting_when_the_gap_moved_on_sends_no_reminder(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    gap = await gaps.get(db_session, bot.id, 1)
    gap.status = "resolved"
    await db_session.commit()
    done = await ar.reject(db_session, row.id, actor=manager, note="不批")
    await db_session.commit()
    assert done.status == "rejected"
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"
    assert await _reminders(db_session, bot.id) == []


async def test_approved_but_still_short_reopens_and_tells(db_session: AsyncSession) -> None:
    """批了、写完授权，级别仍不够缺口（没被解决）：缺口放回 open，交回一条提醒。"""
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=True, reason="改订单", actor=owner)
    await db_session.commit()
    done, _, _ = await ar.mark_approved(db_session, row.id, actor=manager)
    assert await ar.settle_approved(db_session, done)
    await db_session.commit()
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"
    told = await _reminders(db_session, bot.id)
    assert [t["reminder"] for t in told] == ["gap_approved_short"]
    assert told[0]["followups"] == ["F001"]


async def test_approved_and_resolved_settles_quietly(db_session: AsyncSession) -> None:
    bot, owner, manager = await _setup(db_session, allowed=[])
    row = await ar.open_request(db_session, bot, 1, write=False, reason="查订单", actor=owner)
    await db_session.commit()
    done, _, _ = await ar.mark_approved(db_session, row.id, actor=manager)
    await gaps.resolve(db_session, bot.id, "business_system", {"orders": 1})
    assert not await ar.settle_approved(db_session, done)
    await db_session.commit()
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"
    assert [t["reminder"] for t in await _reminders(db_session, bot.id)] == ["gap_resolved"]
