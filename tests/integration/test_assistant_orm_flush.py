"""经 ORM 写助理的内容列：按归属封存，不看行上有没有还原标记；普通机器人不变。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import FeishuSentMessage, Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
PAYLOAD = {"message": {"text": SECRET}, "stale_hold": True}


async def _task_row(session: AsyncSession, task_id: int) -> dict[str, Any]:
    text_ = await raw(
        session,
        "SELECT json_build_object('payload', payload, 'result', result,"
        " 'error_message', error_message)::text FROM tasks WHERE id = :id",
        id=task_id,
    )
    return dict(json.loads(text_))


async def _sent_text(session: AsyncSession, message_id: str) -> str:
    return str(
        await raw(
            session, "SELECT text FROM feishu_sent_messages WHERE message_id = :m", m=message_id
        )
    )


async def _empty_task(session: AsyncSession, bot_id: uuid.UUID) -> int:
    """直接插一行内容为空、没有封装的任务：读出时拿不到任何还原标记。"""
    task_id = (
        await session.execute(
            insert(Task).values(bot_id=bot_id, kind="chat", payload={}).returning(Task.id)
        )
    ).scalar_one()
    await session.commit()
    return int(task_id)


async def _fill(session: AsyncSession, task_id: int) -> Task:
    task = await session.get(Task, task_id, populate_existing=True)
    assert task is not None
    task.payload = dict(PAYLOAD)
    task.result = {"answer": SECRET}
    task.error_message = SECRET
    await session.commit()
    return task


async def _add_rows(session: AsyncSession, bot_id: uuid.UUID) -> tuple[Task, FeishuSentMessage]:
    task = Task(bot_id=bot_id, kind="chat", payload=dict(PAYLOAD))
    sent = FeishuSentMessage(
        bot_id=bot_id, message_id=f"om_{uuid.uuid4().hex[:8]}", chat_id="oc_1", text=SECRET
    )
    session.add_all([task, sent])
    await session.commit()
    return task, sent


def _assert_task_sealed(stored: dict[str, Any]) -> None:
    assert SECRET not in json.dumps(stored, ensure_ascii=False)
    assert stored["payload"]["_sealed"].startswith("enc:v2:d1:")
    assert stored["payload"]["stale_hold"] is True


async def test_locked_row_changed_via_orm_leaves_a_tombstone(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    values = await sealed.seal_values(
        db_session, Task, bot.id, {"bot_id": bot.id, "kind": "chat", "payload": PAYLOAD}
    )
    task_id = int(
        (await db_session.execute(insert(Task).values(**values).returning(Task.id))).scalar_one()
    )
    await db_session.commit()
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    task = await db_session.get(Task, task_id, populate_existing=True)
    assert task is not None and sealed.is_locked(task)
    task.payload = {**task.payload, "note": SECRET, "busy_offer": True}
    task.result = {"answer": SECRET}
    task.error_message = SECRET
    await db_session.commit()
    stored = await _task_row(db_session, task_id)
    assert stored == {
        "payload": {"_sealed": "", "stale_hold": True, "busy_offer": True},
        "result": {"_sealed": ""},
        "error_message": None,
    }
    # 内存里同样置空，仍是已锁定，且不脏。
    assert task.payload == {"stale_hold": True, "busy_offer": True}
    assert task.result is None and task.error_message is None
    assert sealed.is_locked(task) and task not in db_session.dirty


async def test_row_read_with_empty_content_is_sealed_on_assignment(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    task_id = await _empty_task(db_session, bot.id)
    task = await _fill(db_session, task_id)
    stored = await _task_row(db_session, task_id)
    _assert_task_sealed(stored)
    assert set(stored["result"]) == {"_sealed"} and stored["error_message"].startswith("enc:v2:d1:")
    assert task.payload == PAYLOAD and task.error_message == SECRET
    assert task not in db_session.dirty
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None and got.payload == PAYLOAD
        assert got.result == {"answer": SECRET} and got.error_message == SECRET


async def test_rows_added_via_orm_are_sealed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    task, sent = await _add_rows(db_session, bot.id)
    _assert_task_sealed(await _task_row(db_session, task.id))
    text_ = await _sent_text(db_session, sent.message_id)
    assert text_.startswith("enc:v2:d1:") and SECRET not in text_
    assert task.payload == PAYLOAD and sent.text == SECRET
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task.id)
        assert got is not None and got.payload == PAYLOAD
        got_sent = await other.get(FeishuSentMessage, (bot.id, sent.message_id))
        assert got_sent is not None and got_sent.text == SECRET


async def test_added_row_without_a_key_drops_content(db_session: AsyncSession) -> None:
    # 钩子只取用、不签发：所有者还没有密钥时内容丢弃，不落明文。
    bot, _ = await seed_assistant(db_session)
    task, sent = await _add_rows(db_session, bot.id)
    stored = await _task_row(db_session, task.id)
    assert stored["payload"] == {"_sealed": "", "stale_hold": True}
    assert await _sent_text(db_session, sent.message_id) == ""


async def test_standard_bot_rows_stay_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _empty_task(db_session, bot.id)
    await _fill(db_session, task_id)
    stored = await _task_row(db_session, task_id)
    assert stored == {"payload": PAYLOAD, "result": {"answer": SECRET}, "error_message": SECRET}
    # 已读出的行再改一次。
    task = await db_session.get(Task, task_id, populate_existing=True)
    assert task is not None
    task.payload = {**task.payload, "note": SECRET}
    await db_session.commit()
    assert (await _task_row(db_session, task_id))["payload"] == {**PAYLOAD, "note": SECRET}
    added, sent = await _add_rows(db_session, bot.id)
    assert (await _task_row(db_session, added.id))["payload"] == PAYLOAD
    assert await _sent_text(db_session, sent.message_id) == SECRET
    assert not sealed.is_opened(added) and not sealed.is_locked(added)


async def test_cold_cache_still_seals(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    task_id = await _empty_task(db_session, bot.id)
    # 进程内没有任何归属或密钥缓存：钩子自己用同步连接查。
    keys.forget()
    sealed.forget()
    await _fill(db_session, task_id)
    _assert_task_sealed(await _task_row(db_session, task_id))
    keys.forget()
    sealed.forget()
    task, sent = await _add_rows(db_session, bot.id)
    _assert_task_sealed(await _task_row(db_session, task.id))
    assert (await _sent_text(db_session, sent.message_id)).startswith("enc:v2:d1:")


@pytest.fixture
def statements(db_engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(db_engine.sync_engine, "before_cursor_execute", record)


async def test_standard_bot_flush_makes_no_lookup_once_cached(
    db_session: AsyncSession, statements: list[str]
) -> None:
    bot = await seed_standard(db_session)
    task_id = await _empty_task(db_session, bot.id)
    task = await _fill(db_session, task_id)
    # 第一次 flush 查过归属并缓存了「不加密」。
    assert any("FROM bots" in s for s in statements)
    statements.clear()
    task.payload = {**task.payload, "note": "x"}
    task.error_message = "boom"
    await db_session.commit()
    await _add_rows(db_session, bot.id)
    assert any(s.startswith("UPDATE tasks") for s in statements)
    assert not [s for s in statements if "FROM bots" in s or "assistant_keys" in s]
