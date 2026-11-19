"""「从现在开始」：清掉积压的待跟进、子任务、待批审批单、未交回的消息，历史与知识页留着。"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery_checks, followups, gaps, handover, notes
from coreman.core.db.models import (
    AssistantApproval,
    AssistantGap,
    AssistantInbox,
    AssistantJob,
    AssistantReport,
    BusinessSystem,
    SystemAccessRequest,
)
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, code
from tests.integration.job_helpers import owner_with_key, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
RESET = f"{URL}/reset"
PAGE = "规则/口吻"


async def test_the_owner_starts_fresh_and_history_stays(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owner_with_key(db_session)
    await followups.add(db_session, bot.id, category="remind", title="续约")
    later = await followups.add(db_session, bot.id, category="waiting", title="等报价")
    today = date.today()
    await followups.snooze(
        db_session, bot.id, later.seq, until=today + timedelta(days=3), today=today
    )
    finished = await followups.add(db_session, bot.id, category="promised", title="已办完")
    await followups.close(db_session, bot.id, finished.seq)
    await notes.write(db_session, bot.id, PAGE, body="回复要短。", summary="口吻", pinned=True)
    db_session.add(
        AssistantApproval(
            bot_id=bot.id,
            tool="feishu_im_send_message",
            args_enc={"abstract": "私聊 → 李四", "calls": []},
            args_hash=uuid.uuid4().hex,
            request_hash=uuid.uuid4().hex,
            channel="p2p",
            external_id=f"cm-pa-{uuid.uuid4().hex}",
            expires_at=utcnow() + timedelta(hours=72),
            status="pending",
        )
    )
    db_session.add(AssistantReport(bot_id=bot.id, text_enc="旧的进展"))
    await delivery_checks.add_reminder(
        db_session,
        bot.id,
        "old",
        level="batch",
        now=utcnow(),
        summary={"reminder": "weekly_review"},
    )
    job = await spawned(db_session, bot, title="退货原因", brief="查一下")
    await db_session.commit()
    bot_id, job_id, seqs = bot.id, job.id, (1, later.seq, finished.seq)
    await login_existing(client, db_session, owner)

    response = await client.post(RESET, json={"confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "followups": 2,
        "jobs": 1,
        "approvals": 1,
        "inbox": 1,
        "reports": 1,
        "gaps": 0,
    }
    db_session.expire_all()
    statuses = [(await followups.get(db_session, bot_id, seq)).status for seq in seqs]
    assert statuses == ["dropped", "dropped", "done"]
    row = await db_session.get(AssistantJob, job_id, populate_existing=True)
    assert row is not None
    assert row.status == "cancelled" and row.report_code == "not_needed"
    [approval] = list(
        await db_session.scalars(
            select(AssistantApproval).where(AssistantApproval.bot_id == bot_id)
        )
    )
    assert approval.status == "withdrawn"
    assert (
        list(
            await db_session.scalars(
                select(AssistantReport).where(AssistantReport.bot_id == bot_id)
            )
        )
        == []
    )
    inbox = list(
        await db_session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot_id)
            .order_by(AssistantInbox.id)
        )
    )
    assert [(item.status, item.void_code) for item in inbox] == [
        ("void", "owner_reset"),
        ("pending", None),
    ]
    # 唯一还待交回的是告诉助理重新开始的那条提醒；知识页原样留着。
    assert inbox[1].kind == "reminder" and inbox[1].message_id.startswith("owner_reset:")
    page = await notes.read(db_session, bot_id, PAGE)
    assert page is not None and page.body == "回复要短。"
    # 没东西可清时照样受理。
    again = await client.post(RESET, json={"confirm": True})
    assert again.status_code == 200 and again.json()["data"]["followups"] == 0


@pytest.mark.parametrize("confirm", ["true", 1, False, None])
async def test_starting_fresh_needs_a_literal_true(
    client: httpx.AsyncClient, db_session: AsyncSession, confirm: object
) -> None:
    bot, owner = await owner_with_key(db_session)
    await followups.add(db_session, bot.id, category="remind", title="续约")
    await db_session.commit()
    bot_id = bot.id
    await login_existing(client, db_session, owner)
    assert (await client.post(RESET, json={"confirm": confirm})).status_code == 422
    assert (await client.post(RESET, json={})).status_code == 422
    db_session.expire_all()
    assert (await followups.get(db_session, bot_id, 1)).status == "open"


async def test_an_assistant_that_is_not_active_cannot_start_fresh(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owner_with_key(db_session)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    response = await client.post(RESET, json={"confirm": True})
    assert response.status_code == 409 and code(response) == "assistant_not_active"


def test_the_assistant_is_told_to_treat_history_as_background() -> None:
    text = "\n".join(handover._reminder({"reminder": "owner_reset"}))
    assert "只作背景" in text and "不要再跟进" in text
    assert handover.REMINDERS["owner_reset"]


async def test_starting_fresh_removes_unresolved_gaps_and_withdraws_requests(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owner_with_key(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=[]))
    await followups.add(db_session, bot.id, category="promised", title="查订单")
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[1]
    )
    await gaps.report(
        db_session, bot.id, kind="other", key="repo-access", need="读仓库", followup_seqs=[1]
    )
    await gaps.report(
        db_session, bot.id, kind="other", key="old-tool", need="早就开了", followup_seqs=[1]
    )
    await gaps.decline(db_session, bot.id, 3)
    await db_session.commit()
    bot_id = bot.id
    await login_existing(client, db_session, owner)
    requested = await client.post(f"{URL}/gaps/G001/request", json={"reason": "查客户订单状态"})
    assert requested.status_code == 200, requested.text

    response = await client.post(RESET, json={"confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["gaps"] == 2
    db_session.expire_all()
    left = list(
        await db_session.scalars(select(AssistantGap.seq).where(AssistantGap.bot_id == bot_id))
    )
    assert left == [3]
    [request] = list(
        await db_session.scalars(
            select(SystemAccessRequest).where(SystemAccessRequest.bot_id == bot_id)
        )
    )
    assert request.status == "withdrawn" and request.gap_id is None
    board = (await client.get(f"{URL}/board")).json()["data"]
    assert board["gaps"] == []
    # 真缺的以后重新登记，编号接着往后排。
    await followups.add(db_session, bot_id, category="promised", title="再查订单")
    row, outcome = await gaps.report(
        db_session, bot_id, kind="business_system", key="orders", need="查订单", followup_seqs=[2]
    )
    assert outcome == "created" and row.status == "open" and row.seq == 4
