"""assistant_transcripts：转录在库里是密文，密钥销毁后读不出来，普通机器人的行原样存取。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantTranscript
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
BLOB = b"\x1f\x8b-transcript-" + SECRET.encode()


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_transcripts") < BUSINESS_TABLES.index("bots")


async def test_blob_is_sealed_and_reads_back(db_engine: AsyncEngine, db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    sid = uuid.uuid4()
    db_session.add(
        AssistantTranscript(bot_id=bot.id, session_id=sid, blob_enc=BLOB, size=len(BLOB))
    )
    await db_session.commit()
    stored = await raw(
        db_session, "SELECT blob_enc FROM assistant_transcripts WHERE session_id = :s", s=sid
    )
    assert bytes(stored).startswith(b"enc:v2:d1:") and SECRET.encode() not in bytes(stored)
    async with make_session_factory(db_engine)() as other:
        row = await other.get(AssistantTranscript, (bot.id, sid))
        assert row is not None and row.blob_enc == BLOB
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantTranscript, (bot.id, sid), populate_existing=True)
        assert locked is not None and sealed.is_locked(locked) and locked.blob_enc is None


async def test_standard_rows_are_left_alone(db_session: AsyncSession):
    """表本身不区分类型；普通机器人从不经过这里，就算有行也原样存取、不生成数据密钥。"""
    bot = await seed_standard(db_session)
    sid = uuid.uuid4()
    db_session.add(AssistantTranscript(bot_id=bot.id, session_id=sid, blob_enc=b"plain"))
    await db_session.commit()
    assert (
        bytes(
            await raw(
                db_session,
                "SELECT blob_enc FROM assistant_transcripts WHERE session_id = :s",
                s=sid,
            )
        )
        == b"plain"
    )
    assert await sealed.key_ready(db_session, bot.id) is False
