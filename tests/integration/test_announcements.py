from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat.announcements import find_announcement
from coreman.core.db.models import Announcement, ChatLog, RelayServer, Task
from coreman.runtime.bus import tasks
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import seed_bot


async def test_priority_window_and_latest(db_session: AsyncSession) -> None:
    bot, relay, _ = await seed_bot(db_session)
    other = RelayServer(name="r2", host="r2.test", clawrelay_port=80, model_provider="claude")
    db_session.add(other)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            Announcement(scope="global", content="G1"),
            Announcement(scope="relay", relay_server_id=other.id, content="R-other"),
            Announcement(
                scope="relay",
                relay_server_id=relay.id,
                content="R1",
                end_at=now - timedelta(minutes=1),
            ),
            Announcement(
                scope="bot", bot_id=bot.id, content="B-future", start_at=now + timedelta(hours=1)
            ),
            Announcement(scope="bot", bot_id=bot.id, content="B-off", is_active=False),
        ]
    )
    await db_session.commit()
    hit = await find_announcement(db_session, bot_id=bot.id, relay_server_id=relay.id, now=now)
    assert hit is not None and hit.content == "G1"  # relay 的过期、bot 的未生效/停用都不算
    db_session.add(Announcement(scope="relay", relay_server_id=relay.id, content="R2"))
    await db_session.commit()
    hit = await find_announcement(db_session, bot_id=bot.id, relay_server_id=relay.id, now=now)
    assert hit is not None and hit.content == "R2"
    db_session.add(Announcement(scope="bot", bot_id=bot.id, content="B1"))
    await db_session.commit()
    db_session.add(Announcement(scope="bot", bot_id=bot.id, content="B2"))
    await db_session.commit()
    hit = await find_announcement(db_session, bot_id=bot.id, relay_server_id=relay.id, now=now)
    assert hit is not None and hit.content == "B2"
    assert (
        await find_announcement(db_session, bot_id=bot.id, relay_server_id=None, now=now)
    ).content == "B2"  # type: ignore[union-attr]


async def test_chat_task_is_intercepted_before_everything(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session, allowed_user_ids=[])
    db_session.add(Announcement(scope="bot", bot_id=bot.id, content="维护中，稍后再试"))
    await db_session.commit()
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "stop")  # 连 stop 都被公告拦
    await run(db_engine, t, fake)
    s = await stream_of(db_session, t.id)
    assert s.is_complete and s.final_text == "维护中，稍后再试"
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded" and row.result == {"announcement": True}
    assert (await db_session.execute(select(ChatLog))).scalars().all() == []
    assert fake.requests == []
    assert isinstance(t, Task)
