import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import interactions
from coreman.core.db.models import Bot, InteractionState, User


async def _bot(session: AsyncSession) -> Bot:
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    b = Bot(
        bot_key="b1",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(b)
    await session.commit()
    return b


async def test_open_state_overwrites_same_scope(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    scope = interactions.choice_scope(bot.id, "zs")
    first = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=scope,
        state={"n": 1},
        task_id_prefix="p1",
    )
    await db_session.commit()
    second = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=scope,
        state={"n": 2},
        task_id_prefix="p2",
    )
    await db_session.commit()
    rows = (await db_session.execute(select(InteractionState))).scalars().all()
    assert [r.id for r in rows] == [second.id] and first.id != second.id
    assert (
        await interactions.find_by_prefix(db_session, bot_id=bot.id, task_id_prefix="p1")
    ) is None
    got = await interactions.get_open(db_session, kind="choice", scope_key=scope)
    assert got is not None and got.state == {"n": 2}


async def test_status_transitions_patch_and_expiry(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    now = datetime.now(UTC)
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key="s",
        state={"idx": 0},
        task_id_prefix="rl",
        expires_at=now + timedelta(minutes=30),
    )
    await db_session.commit()
    await interactions.patch_state(db_session, st.id, {"idx": 1, "extra": "x"})
    await db_session.commit()
    await db_session.refresh(st)
    assert st.state == {"idx": 1, "extra": "x"}
    assert await interactions.set_status(db_session, st.id, "submitted") is True
    assert await interactions.set_status(db_session, st.id, "submitted") is False  # 已不是 open
    await db_session.commit()
    assert await interactions.get_open(db_session, kind="relay_switch", scope_key="s") is None
    late = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="session_switch",
        scope_key="k",
        state={},
        expires_at=now - timedelta(seconds=1),
    )
    await db_session.commit()
    assert (
        await interactions.get_open(db_session, kind="session_switch", scope_key="k", now=now)
        is None
    )
    assert await interactions.expire_due(db_session, now) == 1
    await db_session.commit()
    await db_session.refresh(late)
    assert late.status == "expired"


async def test_clear_helpers(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={},
    )
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={},
    )
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "ls"),
        state={},
    )
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="session_switch",
        scope_key=interactions.session_scope(bot.id, "g1"),
        state={},
    )
    await db_session.commit()
    assert (
        await interactions.clear_for_speaker(db_session, bot_id=bot.id, platform_user_id="zs") == 2
    )
    assert await interactions.clear_for_session(db_session, bot_id=bot.id, session_key="g1") == 1
    await db_session.commit()
    left = (await db_session.execute(select(InteractionState.scope_key))).scalars().all()
    assert left == [interactions.choice_scope(bot.id, "ls")]
    assert isinstance(uuid.uuid4(), uuid.UUID)
