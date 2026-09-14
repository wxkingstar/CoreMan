import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import sessions
from coreman.core.db.models import Bot, ChatSession, User


async def _bot(session: AsyncSession) -> Bot:
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    b = Bot(
        bot_key="bb",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(b)
    await session.commit()
    return b


async def test_ttl_backend_and_speaker_change(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    first = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="g1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=u1,
    )
    await db_session.commit()
    assert first.is_new and not first.speaker_changed
    row = (await db_session.execute(select(ChatSession))).scalar_one()
    assert row.relay_session_id == first.relay_session_id and row.last_speaker_user_id == u1
    again = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="g1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=u2,
    )
    await db_session.commit()
    assert (
        not again.is_new
        and again.relay_session_id == first.relay_session_id
        and again.speaker_changed
    )
    same = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="g1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=u2,
    )
    assert not same.speaker_changed
    codex = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="g1",
        backend="codex",
        ttl_hours=72,
        speaker_user_id=u2,
    )
    await db_session.commit()
    assert codex.is_new and codex.relay_session_id != first.relay_session_id
    row = (await db_session.execute(select(ChatSession))).scalar_one()
    row.last_active_at = datetime.now(UTC) - timedelta(hours=73)
    await db_session.commit()
    expired = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="g1",
        backend="codex",
        ttl_hours=72,
        speaker_user_id=None,
    )
    await db_session.commit()
    assert expired.is_new and expired.relay_session_id != codex.relay_session_id
    assert (
        await sessions.clear(db_session, bot.id, "g1") is True
        and await sessions.clear(db_session, bot.id, "nope") is False
    )
    await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="a",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="b",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    await db_session.commit()
    assert await sessions.clear_bot(db_session, bot.id) == 2
    await db_session.commit()
    assert await sessions.list_for_bot(db_session, bot.id) == []
