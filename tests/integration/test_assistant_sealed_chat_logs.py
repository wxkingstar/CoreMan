"""chat_logs：消息与回复正文加密入库；换运行时带上文、最近会话预览读到明文。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.chat.session_carry import carried_context
from coreman.core.chat.session_switch import recent_sessions
from coreman.core.db.models import Bot, ChatLog
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
RELAY_SESSION = uuid.uuid4()
CONTENT = (
    "SELECT concat_ws('|', message_content, quoted_content, response_content)"
    " FROM chat_logs WHERE task_id = :task_id"
)


def _entry(bot: Bot, user_id: uuid.UUID | None, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=datetime.now(UTC),
        user_id=user_id,
        user_name="张三",
        chat_id="oc_1",
        session_key="oc_1",
        relay_session_id=RELAY_SESSION,
        task_id=1,
        message_content=f"问：{SECRET}",
        quoted_content=f"引：{SECRET}",
        response_content=f"答：{SECRET}",
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def test_turn_is_sealed_from_open_to_finish(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.open_turn(db_session, _entry(bot, owner.id, response_content=None))
    await db_session.commit()
    opened = str(await raw(db_session, CONTENT, task_id=1))
    assert SECRET not in opened and "enc:v2:d1:" in opened
    assert await chat_logs.finish_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    writer = ChatLogWriter(make_session_factory(db_engine))
    assert await writer.amend_response(1, f"改：{SECRET}")
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    row = await db_session.scalar(
        select(ChatLog).where(ChatLog.task_id == 1).execution_options(populate_existing=True)
    )
    assert row is not None and row.status == "success"
    assert row.message_content == f"问：{SECRET}" and row.quoted_content == f"引：{SECRET}"
    assert row.response_content == f"改：{SECRET}"


async def test_carry_and_recent_sessions_read_plaintext(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    # 先提交：首次写入时生成的数据密钥要对另一个会话可见，否则它会等在这一行上。
    await db_session.commit()
    assert await ChatLogWriter(make_session_factory(db_engine)).write(
        _entry(bot, owner.id, task_id=2)
    )
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=1))
    assert SECRET not in str(await raw(db_session, CONTENT, task_id=2))
    carried = await carried_context(
        db_session, bot_id=bot.id, session_key="oc_1", relay_session_id=RELAY_SESSION
    )
    assert carried is not None and f"问：{SECRET}" in carried and f"答：{SECRET}" in carried
    previews = await recent_sessions(
        db_session, bot_id=bot.id, session_key="oc_1", relay_server_id=None
    )
    assert [p.preview for p in previews] == [f"问：{SECRET}"]


async def test_destroyed_key_leaves_nothing_to_carry(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert (
        await carried_context(
            db_session, bot_id=bot.id, session_key="oc_1", relay_session_id=RELAY_SESSION
        )
        is None
    )
    # 销毁之后的轮次照常记一行，正文丢弃。
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id, task_id=3))
    await db_session.commit()
    assert str(await raw(db_session, CONTENT, task_id=3)) == ""


async def test_standard_bot_log_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, None))
    await db_session.commit()
    stored = str(await raw(db_session, CONTENT, task_id=1))
    assert stored == f"问：{SECRET}|引：{SECRET}|答：{SECRET}"


async def test_file_info_and_error_message_ride_along(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    entry = _entry(
        bot, owner.id, status="error", file_info={"name": SECRET}, error_message=f"错：{SECRET}"
    )
    assert await chat_logs.insert_turn(db_session, entry)
    await db_session.commit()
    stored = str(
        await raw(
            db_session,
            "SELECT concat_ws('|', file_info::text, error_message, status, user_name)"
            " FROM chat_logs WHERE task_id = 1",
        )
    )
    assert SECRET not in stored and stored.count("enc:v2:d1:") == 2
    assert stored.endswith("|error|张三")
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == 1))
    assert row is not None and row.file_info == {"name": SECRET}
    assert row.error_message == f"错：{SECRET}"
