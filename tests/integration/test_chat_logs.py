import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat.chat_logs import LIMITS, ChatLogEntry, ChatLogWriter
from coreman.core.db.models import Bot, ChatLog, User, UserReached
from coreman.core.db.session import make_session_factory


def entry(bot: Bot, user_id: uuid.UUID | None, **over: object) -> ChatLogEntry:
    base = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="wecom",
        user_id=user_id,
        platform_user_id="zs",
        user_login="zhangsan",
        user_name="张三",
        chat_type="single",
        chat_id="zs",
        session_key="zs",
        relay_session_id=uuid.uuid4(),
        relay_server_id=None,
        model="m",
        stream_id="s",
        task_id=1,
        message_type="text",
        message_content="x" * 20000,
        quoted_content=None,
        file_info=None,
        response_content="y" * 60000,
        tools_used=["Bash", "Bash"],
        status="success",
        error_code=None,
        error_message=None,
        latency_ms=12,
        input_tokens=0,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        request_at=datetime.now(UTC),
        response_at=datetime.now(UTC),
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def test_write_truncates_dedupes_tools_and_marks_reached(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    u = User(login_name="zhangsan", display_name="张三")
    db_session.add(u)
    await db_session.flush()
    bot = Bot(
        bot_key="bb",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    db_session.add(bot)
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine))
    assert await writer.write(entry(bot, u.id)) is True
    assert await writer.write(entry(bot, u.id)) is True
    log = (await db_session.execute(select(ChatLog).order_by(ChatLog.id))).scalars().first()
    assert (
        log
        and len(log.message_content or "") == LIMITS["message_content"]
        and len(log.response_content or "") == LIMITS["response_content"]
    )
    assert log.tools_used == ["Bash"] and log.input_tokens == 0 and log.output_tokens is None
    reached = (await db_session.execute(select(UserReached))).scalars().all()
    assert len(reached) == 1 and reached[0].platform_chat_id == "zs"
    assert await writer.write(entry(bot, None, user_login=None, user_name=None)) is True
    assert len((await db_session.execute(select(UserReached))).scalars().all()) == 1


async def test_submit_is_fire_and_forget_and_bounded(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    u = User(login_name="c", display_name="c")
    db_session.add(u)
    await db_session.flush()
    bot = Bot(
        bot_key="bb",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    db_session.add(bot)
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine), max_pending=2)
    for _ in range(4):
        writer.submit(entry(bot, None))
    assert writer.dropped >= 1
    await writer.drain(timeout=5)
    n = len((await db_session.execute(select(ChatLog))).scalars().all())
    assert 2 <= n <= 3


async def test_retries_then_gives_up(db_engine: AsyncEngine) -> None:
    calls = 0

    class Boom:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise OSError("db down")

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return False

    writer = ChatLogWriter(lambda: Boom(), sleep=lambda s: asyncio.sleep(0))  # type: ignore[arg-type]
    fake_bot = Bot(
        id=uuid.uuid4(),
        bot_key="bb",
        platform="wecom",
        name="b",
        created_by=uuid.uuid4(),
        model="m",
        working_dir="/d",
        credentials_enc="x",
    )
    assert await writer.write(entry(fake_bot, None)) is False and calls == 3


async def test_group_and_cron_do_not_prove_private_chat_reachability(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from tests.integration.worker_helpers import seed_bot

    bot, _, _ = await seed_bot(db_session)
    writer = ChatLogWriter(make_session_factory(db_engine))
    for kind in ("group", "cron"):
        assert await writer.write(
            entry(bot, bot.created_by, chat_type=kind, chat_id="not-a-private-chat")
        )
    assert await db_session.scalar(select(UserReached)) is None
