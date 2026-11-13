"""cloud_pending_approvals.reason：助理记录的拒绝理由加密入库，普通机器人照旧是明文。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.cloud_proxy import decisions
from coreman.core.db.models import Bot, CloudPendingApproval
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
REASON = "SELECT reason FROM cloud_pending_approvals WHERE external_id = :id"


async def _rejected(session: AsyncSession, bot: Bot, external_id: str) -> None:
    session.add(
        CloudPendingApproval(
            external_id=external_id,
            bot_id=bot.id,
            session_key="",
            product="slb",
            action="AddAccessControlListEntry",
            request_hash="h",
        )
    )
    await session.commit()
    done = await decisions.apply(
        session,
        approval_id="apv-" + external_id,
        external_id=external_id,
        status="rejected",
        reason=f"这个网段太宽-{SECRET}",
        decided_at=None,
    )
    await session.commit()
    assert done.known


async def test_assistant_reason_is_sealed_and_reads_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await _rejected(db_session, bot, "cm-1-aa")
    stored = str(await raw(db_session, REASON, id="cm-1-aa"))
    assert stored.startswith("enc:v2:d1:") and SECRET not in stored
    async with make_session_factory(db_engine)() as other:
        row = await other.get(CloudPendingApproval, "cm-1-aa")
        assert row is not None and row.reason == f"这个网段太宽-{SECRET}"
        assert row.status == "rejected"


async def test_standard_bot_reason_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    await _rejected(db_session, bot, "cm-2-bb")
    assert await raw(db_session, REASON, id="cm-2-bb") == f"这个网段太宽-{SECRET}"
