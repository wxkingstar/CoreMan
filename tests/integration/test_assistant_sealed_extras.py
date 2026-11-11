"""五张表正文列之外的内容：报错信息、附件信息、已发消息的正文存档。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.db.models import Bot, ChatLog
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from coreman.runtime.scheduler.reaper import close_abandoned_turns
from coreman.runtime.worker.chat import feishu_quote
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
ERROR = f"RuntimeError: 处理「{SECRET}」时出错"


async def _task(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    await session.commit()
    assert task is not None
    return task.id


def _entry(bot: Bot, user_id: uuid.UUID | None, task_id: int, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="file",
        status="error",
        request_at=datetime.now(UTC),
        user_id=user_id,
        chat_id="oc_1",
        session_key="oc_1",
        task_id=task_id,
        message_content="看一下这个文件",
        file_info={"filename": f"{SECRET}.pdf", "size": 12},
        error_code="handler_crashed",
        error_message=ERROR,
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def _log_text(session: AsyncSession, task_id: int) -> str:
    return str(
        await raw(
            session,
            "SELECT concat_ws('|', file_info::text, error_message, error_code)"
            " FROM chat_logs WHERE task_id = :id",
            id=task_id,
        )
    )


async def test_task_error_message_is_sealed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    long_error = ERROR + "x" * 6000
    assert await tasks.finish(
        db_session, task_id, status="failed", error_code="handler_crashed", error_message=long_error
    )
    await db_session.commit()
    stored = str(
        await raw(
            db_session,
            "SELECT error_code || '|' || error_message FROM tasks WHERE id = :id",
            id=task_id,
        )
    )
    assert stored.startswith("handler_crashed|enc:v2:d1:") and SECRET not in stored
    async with make_session_factory(db_engine)() as other:
        got = await tasks.get(other, task_id)
        # 截断按明文长度做，在加密之前。
        assert got is not None and got.error_message == long_error[:5000]


async def test_chat_log_file_info_and_error_are_sealed(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id, task_id))
    await db_session.commit()
    stored = await _log_text(db_session, task_id)
    assert SECRET not in stored and stored.endswith("|handler_crashed")
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.error_message == ERROR
    assert row.file_info == {"filename": f"{SECRET}.pdf", "size": 12}


async def test_close_running_seals_the_error(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    opening = _entry(bot, owner.id, task_id, error_code=None, error_message=None)
    assert await chat_logs.open_turn(db_session, opening)
    await db_session.commit()
    assert await chat_logs.close_running(
        db_session, task_id, status="error", error_code="handler_crashed", error_message=ERROR
    )
    await db_session.commit()
    assert SECRET not in await _log_text(db_session, task_id)
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.status == "error" and row.error_message == ERROR
    assert row.message_content == "看一下这个文件"


async def test_reaper_copies_the_task_error_into_the_log(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _task(db_session, bot)
    opening = _entry(bot, owner.id, task_id, error_code=None, error_message=None)
    assert await chat_logs.open_turn(db_session, opening)
    await tasks.finish(
        db_session, task_id, status="failed", error_code="handler_crashed", error_message=ERROR
    )
    await db_session.execute(
        text("UPDATE tasks SET finished_at = now() - interval '1 day' WHERE id = :id"),
        {"id": task_id},
    )
    await db_session.commit()
    assert await close_abandoned_turns(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    assert SECRET not in await _log_text(db_session, task_id)
    row = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task_id))
    assert row is not None and row.status == "error" and row.error_code == "handler_crashed"
    assert row.error_message == ERROR


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_sent_message_archive(
    db_engine: AsyncEngine, db_session: AsyncSession, seed: str
) -> None:
    owner = None
    if seed == "assistant":
        bot, owner = await seed_assistant(db_session)
    else:
        bot = await seed_standard(db_session)
    # remember 只用到 factory 与 bot_id，不需要一条真的飞书连接。
    transport = SimpleNamespace(factory=make_session_factory(db_engine), bot_id=bot.id)
    await FeishuTransport.remember(transport, "om_5", "oc_1", f"答：{SECRET}", work_code="W-1")  # type: ignore[arg-type]
    stored = str(
        await raw(
            db_session,
            "SELECT work_code || '|' || text FROM feishu_sent_messages WHERE message_id = 'om_5'",
        )
    )
    if owner is None:
        assert stored == f"W-1|答：{SECRET}"
    else:
        assert stored.startswith("W-1|enc:v2:d1:") and SECRET not in stored
    assert (
        await feishu_quote.persisted_text(db_session, bot=bot, chat_id="oc_1", parent_id="om_5")
        == f"答：{SECRET}"
    )
    if owner is not None:
        # 销毁之后引用这条消息：存档读不出来，按「没有存档」处理。
        await keys.destroy_key(db_session, owner.id)
        await db_session.commit()
        assert (
            await feishu_quote.persisted_text(db_session, bot=bot, chat_id="oc_1", parent_id="om_5")
            is None
        )


async def test_standard_bot_errors_stay_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _task(db_session, bot)
    await tasks.finish(db_session, task_id, status="failed", error_message=ERROR)
    assert await chat_logs.insert_turn(db_session, _entry(bot, None, task_id))
    await db_session.commit()
    assert (
        await raw(db_session, "SELECT error_message FROM tasks WHERE id = :id", id=task_id) == ERROR
    )
    stored = await _log_text(db_session, task_id)
    assert SECRET in stored and "enc:v2:" not in stored and "_sealed" not in stored
