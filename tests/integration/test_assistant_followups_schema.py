"""assistant_followups：约束，以及标题与备注在库里是密文。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantFollowup
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = "SELECT title_enc || coalesce(notes_enc, '') FROM assistant_followups WHERE id = :id"


def _row(bot_id: uuid.UUID, seq: int = 1, **kw: object) -> AssistantFollowup:
    values: dict[str, object] = {
        "bot_id": bot_id,
        "seq": seq,
        "category": "promised",
        "title_enc": f"周四前给李四报价 {SECRET}",
    }
    return AssistantFollowup(**{**values, **kw})


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_followups") < BUSINESS_TABLES.index("bots")


async def test_title_and_notes_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _row(bot.id, notes_enc=f"备注-{SECRET}", due_on=date(2026, 11, 19))
    db_session.add(row)
    await db_session.commit()
    assert row.status == "open" and row.closed_at is None and row.job_id is None
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "李四" not in stored and stored.count("enc:v2:d1:") == 2
    # 分类、状态、日期是明文：按日期查询不用解密。
    assert (
        await raw(
            db_session,
            "SELECT category || ':' || status || ':' || due_on::text FROM assistant_followups"
            " WHERE id = :id",
            id=row.id,
        )
        == "promised:open:2026-11-19"
    )
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantFollowup, row.id)
        assert found is not None and found.title_enc == f"周四前给李四报价 {SECRET}"
        assert found.notes_enc == f"备注-{SECRET}"
        # 密钥销毁之后：行还在、元数据可读，内容为空且不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantFollowup, row.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.title_enc == "" and locked.notes_enc is None
        assert (locked.seq, locked.category, locked.due_on) == (1, "promised", date(2026, 11, 19))


async def test_category_status_and_close_code_are_constrained(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    bot_id = bot.id
    for bad in ({"category": "todo"}, {"status": "closed"}, {"close_code": "because"}):
        db_session.add(_row(bot_id, **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
    db_session.add(_row(bot_id, status="dropped", close_code="origin_recalled"))
    await db_session.flush()


async def test_seq_is_unique_within_one_assistant(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other, other_owner = await seed_assistant(db_session)
    for user in (owner, other_owner):
        await keys.ensure_key(db_session, sealed.master(), user.id)
    bot_id = bot.id
    # 两位助理各有自己的 F001。
    db_session.add_all([_row(bot_id, 1), _row(other.id, 1)])
    await db_session.commit()
    db_session.add(_row(bot_id, 1))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
