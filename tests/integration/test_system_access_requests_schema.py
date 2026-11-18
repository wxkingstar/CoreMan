"""system_access_requests：同一助理同一系统只有一张在途申请；随缺口删除置空；审批单可以挂在待跟进名下。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.db.models import AssistantApproval, BusinessSystem, SystemAccessRequest
from coreman.core.timeutils import utcnow
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


def test_table_is_truncated() -> None:
    assert "system_access_requests" in BUSINESS_TABLES


async def test_one_pending_request_per_system(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=[]))
    await db_session.flush()

    def row(status: str = "pending") -> SystemAccessRequest:
        return SystemAccessRequest(
            bot_id=bot.id,
            system_key="orders",
            reason="查订单",
            requested_by=owner.id,
            status=status,
        )

    db_session.add_all([row(), row("rejected")])
    await db_session.commit()
    db_session.add(row())
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_approval_can_point_at_a_followup(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    approval = AssistantApproval(
        bot_id=bot.id,
        tool="feishu_im_send_message",
        args_enc={"abstract": "私聊 → 李四", "calls": []},
        args_hash=uuid.uuid4().hex,
        request_hash=uuid.uuid4().hex,
        channel="p2p",
        external_id=f"cm-pa-{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(hours=72),
        followup_id=item.id,
    )
    db_session.add(approval)
    await db_session.commit()
    await db_session.delete(item)
    await db_session.commit()
    await db_session.refresh(approval)
    assert approval.followup_id is None
