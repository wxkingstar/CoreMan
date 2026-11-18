"""assistant_gaps：种类与状态约束、按种类与标识唯一、要什么在库里是密文、待跟进的挂靠随缺口删除置空。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.db.models import AssistantFollowup, AssistantGap
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


def _gap(bot_id, seq=1, **kw) -> AssistantGap:  # type: ignore[no-untyped-def]
    values = {"bot_id": bot_id, "seq": seq, "kind": "business_system", "key": "orders"}
    return AssistantGap(**{**values, "need_enc": f"查订单 {SECRET}", **kw})


def test_table_is_truncated() -> None:
    assert "assistant_gaps" in BUSINESS_TABLES


async def test_need_is_sealed_and_key_is_unique(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    gap = _gap(bot.id)
    db_session.add(gap)
    await db_session.commit()
    assert gap.status == "open" and gap.resolved_at is None
    stored = str(
        await raw(db_session, "SELECT need_enc FROM assistant_gaps WHERE id = :id", id=gap.id)
    )
    assert SECRET not in stored and stored.startswith("enc:v2:d1:")
    db_session.add(_gap(bot.id, seq=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("field", [{"kind": "repo"}, {"status": "closed"}])
async def test_kind_and_status_are_checked(db_session: AsyncSession, field: dict) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(_gap(bot.id, **field))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_followup_link_is_cleared_when_the_gap_goes(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    gap = _gap(bot.id)
    db_session.add(gap)
    await db_session.flush()
    row = await followups.add(db_session, bot.id, category="promised", title="查退货")
    row.gap_id = gap.id
    await db_session.commit()
    await db_session.delete(gap)
    await db_session.commit()
    fresh = await db_session.get(AssistantFollowup, row.id, populate_existing=True)
    assert fresh is not None and fresh.gap_id is None
