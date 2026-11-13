"""assistant_pages：约束，以及四列内容在库里是密文。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import PAGE_AUTHORS, AssistantPage
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT path_enc || title_enc || summary_enc || body_enc FROM assistant_pages WHERE id = :id"
)


def _page(bot_id: uuid.UUID, **kw: object) -> AssistantPage:
    values: dict[str, object] = {
        "bot_id": bot_id,
        "path_hmac": "h1",
        "path_enc": f"people/{SECRET}.md",
        "title_enc": f"标题-{SECRET}",
        "summary_enc": f"摘要-{SECRET}",
        "body_enc": f"正文-{SECRET}",
        "size": 20,
    }
    return AssistantPage(**{**values, **kw})


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_pages") < BUSINESS_TABLES.index("bots")
    assert PAGE_AUTHORS == ("owner", "assistant", "import")


async def test_content_is_sealed_and_reads_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _page(bot.id)
    db_session.add(row)
    await db_session.commit()
    assert row.pinned is False and row.version == 1 and row.updated_by == "assistant"
    assert row.updated_at is not None
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "people/" not in stored
    assert stored.count("enc:v2:d1:") == 4
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantPage, row.id)
        assert found is not None
        assert found.path_enc == f"people/{SECRET}.md" and found.title_enc == f"标题-{SECRET}"
        assert found.summary_enc == f"摘要-{SECRET}" and found.body_enc == f"正文-{SECRET}"
        # 密钥销毁之后：行还在、元数据可读，内容为空串且不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantPage, row.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.path_enc == "" and locked.body_enc == ""
        assert locked.version == 1 and locked.size == 20


async def test_path_is_unique_per_assistant(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other, other_owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await keys.ensure_key(db_session, sealed.master(), other_owner.id)
    bot_id, other_id = bot.id, other.id
    db_session.add_all([_page(bot_id), _page(other_id)])
    await db_session.flush()
    db_session.add(_page(bot_id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_updated_by_is_constrained(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(_page(bot.id, updated_by="someone"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_standard_bot_rows_stay_plain(db_session: AsyncSession) -> None:
    """封存按记录的类型走：普通机器人的行原样落库（知识库本身只给助理用，见 notes.py）。"""
    bot = await seed_standard(db_session)
    row = _page(bot.id)
    db_session.add(row)
    await db_session.commit()
    assert SECRET in str(await raw(db_session, STORED, id=row.id))
