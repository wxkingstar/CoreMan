"""Core 语句写入封存值之后，同一会话里早先取出的对象上读到的仍是明文（不用重取）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Task, TaskStream
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = "SELECT concat_ws('|', result::text, error_message) FROM tasks WHERE id = :id"


@pytest.mark.parametrize("kind", ["assistant", "standard"])
async def test_loaded_task_reads_plain_values_after_finish(
    db_session: AsyncSession, kind: str
) -> None:
    bot = (
        (await seed_assistant(db_session))[0]
        if kind == "assistant"
        else await seed_standard(db_session)
    )
    queued = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    assert queued is not None
    await db_session.commit()
    task = await db_session.get(Task, queued.id)
    assert task is not None and task.result is None
    result, message = {"reply": SECRET}, f"出错：{SECRET}"
    assert await tasks.finish(
        db_session, task.id, status="failed", result=result, error_message=message, bot_id=bot.id
    )
    # 不重取，直接读同一个对象。
    assert task.status == "failed"
    assert task.result == result and task.error_message == message
    await db_session.commit()
    assert task.result == result and task.error_message == message
    stored = str(await raw(db_session, STORED, id=task.id))
    assert (SECRET in stored) == (kind == "standard")
    again = await db_session.get(Task, task.id)
    assert again is task and again.result == result and again.error_message == message


@pytest.mark.parametrize("kind", ["assistant", "standard"])
async def test_loaded_stream_reads_plain_values_after_update(
    db_session: AsyncSession, kind: str
) -> None:
    bot = (
        (await seed_assistant(db_session))[0]
        if kind == "assistant"
        else await seed_standard(db_session)
    )
    queued = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    assert queued is not None
    row = await streams.create(
        db_session,
        task_id=queued.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await db_session.commit()
    plan = [{"text": SECRET, "status": "pending"}]
    await streams.update(db_session, queued.id, bot_id=bot.id, pending_text=SECRET, plan=plan)
    assert row.pending_text == SECRET and row.plan == plan
    await streams.complete(
        db_session, queued.id, final_text=f"终：{SECRET}", pending_card={"body": SECRET}
    )
    assert row.final_text == f"终：{SECRET}" and row.pending_card == {"body": SECRET}
    await db_session.commit()
    again = await db_session.get(TaskStream, queued.id)
    assert again is row and again.final_text == f"终：{SECRET}" and again.pending_text == SECRET
    stored = str(
        await raw(
            db_session,
            "SELECT concat_ws('|', pending_text, final_text, pending_card::text, plan::text)"
            " FROM task_streams WHERE task_id = :id",
            id=queued.id,
        )
    )
    assert (SECRET in stored) == (kind == "standard")


async def test_loaded_task_gets_the_blank_value_once_the_key_is_destroyed(
    db_session: AsyncSession,
) -> None:
    bot, owner = await seed_assistant(db_session)
    queued = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    assert queued is not None
    await db_session.commit()
    task = await db_session.get(Task, queued.id)
    assert task is not None
    await keys.destroy_key(db_session, owner.id)
    await tasks.finish(
        db_session, task.id, status="failed", result={"reply": SECRET}, error_message=SECRET
    )
    # 内容已丢弃：对象上是空值，不是墓碑。
    assert task.status == "failed" and task.result is None and task.error_message is None
