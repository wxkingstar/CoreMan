import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat import interactions
from coreman.core.chat.session_switch import recent_sessions
from coreman.core.db.models import ChatLog, ChatSession, InteractionState, RelayServer
from coreman.core.i18n.messages import msg
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import seed_bot


def _log(
    bot,  # type: ignore[no-untyped-def]
    *,
    rs: uuid.UUID,
    at: datetime,
    text: str | None,
    mtype: str = "text",
    relay_id=None,  # type: ignore[no-untyped-def]
    session_key="zs",  # type: ignore[no-untyped-def]
) -> ChatLog:
    return ChatLog(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="wecom",
        chat_type="single",
        chat_id=session_key,
        session_key=session_key,
        relay_session_id=rs,
        relay_server_id=relay_id,
        message_type=mtype,
        message_content=text,
        status="success",
        request_at=at,
    )


async def test_recent_sessions_query(db_session: AsyncSession) -> None:
    bot, relay, _ = await seed_bot(db_session)
    other = RelayServer(name="other", host="o.test", clawrelay_port=80, model_provider="claude")
    db_session.add(other)
    await db_session.flush()
    now = datetime.now(UTC)
    s1, s2, s3, s4 = (uuid.uuid4() for _ in range(4))
    db_session.add_all(
        [
            _log(
                bot,
                rs=s1,
                at=now - timedelta(days=1),
                text="第一句很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长",
                relay_id=relay.id,
            ),
            _log(bot, rs=s1, at=now - timedelta(hours=1), text="后续", relay_id=relay.id),
            _log(
                bot, rs=s2, at=now - timedelta(minutes=10), text=None, mtype="image", relay_id=None
            ),
            _log(bot, rs=s3, at=now - timedelta(minutes=5), text="别的 relay", relay_id=other.id),
            _log(
                bot,
                rs=s4,
                at=now - timedelta(minutes=1),
                text="别的会话键",
                relay_id=relay.id,
                session_key="ls",
            ),
        ]
    )
    await db_session.commit()
    items = await recent_sessions(
        db_session, bot_id=bot.id, session_key="zs", relay_server_id=relay.id
    )
    assert [i.relay_session_id for i in items] == [s2, s1]
    assert items[0].preview == msg("non_text_message") and len(items[1].preview) == 30


async def test_list_then_switch_by_number(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, relay, _ = await seed_bot(db_session)
    now = datetime.now(UTC)
    old, newer = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            _log(bot, rs=old, at=now - timedelta(hours=2), text="上午的话题", relay_id=relay.id),
            _log(
                bot, rs=newer, at=now - timedelta(minutes=3), text="刚才的话题", relay_id=relay.id
            ),
        ]
    )
    await db_session.commit()
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "sessions")
    await run(db_engine, t, fake)
    s = await stream_of(db_session, t.id)
    assert (
        s.final_text.startswith(msg("sessions_header", n=2))
        and "1. 刚才的话题" in s.final_text
        and "2. 上午的话题" in s.final_text
    )
    assert fake.requests == []
    st = await interactions.get_open(
        db_session, kind="session_switch", scope_key=interactions.session_scope(bot.id, "zs")
    )
    assert st is not None and [x["relay_session_id"] for x in st.state["sessions"]] == [
        str(newer),
        str(old),
    ]
    t2 = await chat_task(db_session, bot, "2")
    await run(db_engine, t2, fake)
    assert (await stream_of(db_session, t2.id)).final_text == msg(
        "session_switched", index=2, preview="上午的话题"
    )
    row = await db_session.get(ChatSession, (bot.id, "zs"))
    assert row and row.relay_session_id == old
    assert fake.requests == []
    # 列表与切换都不是一轮对话：除了造数据那两条，不该多出任何 chat_logs。
    assert len((await db_session.execute(select(ChatLog))).scalars().all()) == 2
    await db_session.refresh(st)
    assert st.status == "submitted"
    # 之后再发数字：没有待选状态 → 普通消息进 AI，且会话沿用刚切的那个。
    t3 = await chat_task(db_session, bot, "1")
    await run(db_engine, t3, fake)
    assert len(fake.requests) == 1 and fake.requests[0]["session_id"] == str(old)


async def test_empty_and_out_of_range(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "会话列表")
    await run(db_engine, t, fake)
    assert (await stream_of(db_session, t.id)).final_text == msg("no_sessions")
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    t2 = await chat_task(db_session, bot, "3")
    await run(db_engine, t2, fake)
    assert len(fake.requests) == 1  # 没有列表状态：数字就是普通消息
