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


async def test_carry_over_keeps_moved_sessions_and_hands_others_their_history(
    db_session: AsyncSession,
) -> None:
    from coreman.core.chat.session_carry import carried_context
    from coreman.core.db.models import ChatLog

    bot = await _bot(db_session)
    moved = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="a",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    stuck = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="b",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    now = datetime.now(UTC)
    for i, (text, reply) in enumerate([("第一个问题", "第一个回答"), ("第二个问题", "第二个回答")]):
        db_session.add(
            ChatLog(
                bot_id=bot.id,
                bot_key=bot.bot_key,
                platform="wecom",
                chat_type="single",
                session_key="b",
                relay_session_id=stuck.relay_session_id,
                user_name="张三",
                message_type="text",
                status="success",
                request_at=now + timedelta(seconds=i),
                message_content=text,
                response_content=reply,
            )
        )
    await sessions.carry_over(db_session, bot.id, backend="claude", keep={moved.relay_session_id})
    # 连换两次还没聊过：仍记最初那条会话。
    await sessions.carry_over(db_session, bot.id, backend="claude", keep={moved.relay_session_id})
    await db_session.commit()
    again = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="a",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    # 带过去的原样续用，也记下自己：首轮续接文本交给驱动备用。
    assert again.relay_session_id == moved.relay_session_id
    assert again.inherited_from == moved.relay_session_id
    first = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="b",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    assert first.relay_session_id != stuck.relay_session_id and not first.is_new
    assert first.inherited_from == stuck.relay_session_id
    note = await carried_context(
        db_session, bot_id=bot.id, session_key="b", relay_session_id=first.inherited_from
    )
    assert note is not None
    assert note.index("张三：第一个问题") < note.index("你：第二个回答")
    await db_session.commit()
    # 只在首轮带一次。
    second = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="b",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    assert second.relay_session_id == first.relay_session_id and second.inherited_from is None
