"""内容本身长得像封装（带 `_sealed` 键、像密文的字符串）：照常加密，读回与原文一致。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import OutboxItem, Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
MARKER = "plain-marker"
LOOKALIKE = {"_sealed": MARKER, "x": 1}
# 像墓碑、像密文的内容：还原之后留在内存里，不能再被当成封装解一次。
TRICKY: list[dict[str, Any]] = [
    {"_sealed": ""},
    {"_sealed": "enc:v2:d1:AAAA", "x": 1},
    {"_sealed": 3},
]
TRICKY_TEXT = "enc:v2:d1:AAAA"


async def _task_row(session: AsyncSession, task_id: int) -> str:
    return str(
        await raw(
            session,
            "SELECT concat_ws('|', payload::text, result::text, error_message)"
            " FROM tasks WHERE id = :id",
            id=task_id,
        )
    )


async def _outbox_row(session: AsyncSession, item_id: int) -> str:
    return str(await raw(session, "SELECT payload::text FROM outbox WHERE id = :id", id=item_id))


async def _add_via_orm(session: AsyncSession, bot_id: uuid.UUID) -> tuple[int, int]:
    task = Task(bot_id=bot_id, kind="chat", payload=dict(LOOKALIKE), result=dict(LOOKALIKE))
    item = OutboxItem(
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"k:{uuid.uuid4().hex[:8]}",
        target={"chat_id": "oc_1"},
        payload=dict(LOOKALIKE),
    )
    session.add_all([task, item])
    await session.commit()
    return task.id, item.id


async def _insert_via_core(session: AsyncSession, bot_id: uuid.UUID) -> int:
    values = await sealed.seal_values(
        session,
        Task,
        bot_id,
        {"bot_id": bot_id, "kind": "chat", "payload": dict(LOOKALIKE), "result": dict(LOOKALIKE)},
    )
    task_id = (await session.execute(insert(Task).values(**values).returning(Task.id))).scalar_one()
    await session.commit()
    return int(task_id)


async def test_assistant_content_with_a_sealed_key_is_encrypted(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    task_id, item_id = await _add_via_orm(db_session, bot.id)
    core_id = await _insert_via_core(db_session, bot.id)
    for stored in (
        await _task_row(db_session, task_id),
        await _task_row(db_session, core_id),
        await _outbox_row(db_session, item_id),
    ):
        assert MARKER not in stored and "enc:v2:d1:" in stored
    async with make_session_factory(db_engine)() as other:
        for id_ in (task_id, core_id):
            task = await other.get(Task, id_)
            assert task is not None
            assert task.payload == LOOKALIKE and task.result == LOOKALIKE
        item = await other.get(OutboxItem, item_id)
        assert item is not None and item.payload == LOOKALIKE
        # 经 ORM 改写整值列：新值同样带 `_sealed` 键。
        task = await other.get(Task, task_id)
        assert task is not None
        task.result = {"_sealed": f"{MARKER}-2"}
        await other.commit()
    assert MARKER not in await _task_row(db_session, task_id)
    again = await db_session.get(Task, task_id, populate_existing=True)
    assert again is not None and again.result == {"_sealed": f"{MARKER}-2"}


async def test_standard_bot_content_with_a_sealed_key_is_untouched(
    db_session: AsyncSession,
) -> None:
    bot = await seed_standard(db_session)
    task_id, item_id = await _add_via_orm(db_session, bot.id)
    core_id = await _insert_via_core(db_session, bot.id)
    for id_ in (task_id, core_id):
        stored = await raw(
            db_session,
            "SELECT json_build_array(payload, result)::text FROM tasks WHERE id = :id",
            id=id_,
        )
        assert json.loads(stored) == [LOOKALIKE, LOOKALIKE]
        task = await db_session.get(Task, id_, populate_existing=True)
        assert task is not None and task.payload == LOOKALIKE and task.result == LOOKALIKE
    assert json.loads(await _outbox_row(db_session, item_id)) == LOOKALIKE
    item = await db_session.get(OutboxItem, item_id, populate_existing=True)
    assert item is not None and item.payload == LOOKALIKE


@pytest.mark.parametrize("content", TRICKY)
async def test_opened_lookalike_content_is_not_opened_twice(
    db_engine: AsyncEngine, db_session: AsyncSession, content: dict[str, Any]
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    task = Task(
        bot_id=bot.id,
        kind="chat",
        payload=dict(content),
        result=dict(content),
        error_message=TRICKY_TEXT,
    )
    db_session.add(task)
    await db_session.commit()
    stored = await _task_row(db_session, task.id)
    assert "AAAA" not in stored and stored.count("enc:v2:d1:") == 3
    # 写入之后的这一行、另一个会话读出的那一行：再取一次都不该被当成封装重解。
    again = await db_session.get(Task, task.id)
    assert again is task and task.payload == content and task.error_message == TRICKY_TEXT
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task.id)
        assert got is not None
        for _ in range(2):
            assert await other.get(Task, task.id) is got
            assert got.payload == content and got.result == content
            assert got.error_message == TRICKY_TEXT and not sealed.is_locked(got)
        # 在已还原的行上追加键：整份内容重新加密，不把旧内容当封装。
        got.payload = {**got.payload, "note": MARKER}
        await other.commit()
        assert got.payload == {**content, "note": MARKER}
    assert MARKER not in await _task_row(db_session, task.id)
    fresh = await db_session.get(Task, task.id, populate_existing=True)
    assert fresh is not None and fresh.payload == {**content, "note": MARKER}


async def test_unopened_envelope_changed_via_orm_keeps_its_content(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    task = Task(bot_id=bot.id, kind="chat", payload={"message": MARKER, "stale_hold": True})
    db_session.add(task)
    await db_session.commit()
    async with make_session_factory(db_engine)() as other:
        # scalars() 不自动还原：内存里是库里的封装。
        row = (await other.scalars(select(Task).where(Task.id == task.id))).one()
        assert "message" not in row.payload and not sealed.is_opened(row)
        row.payload = {k: v for k, v in row.payload.items() if k != "stale_hold"} | {"note": MARKER}
        await other.commit()
    stored = await _task_row(db_session, task.id)
    assert MARKER not in stored and "stale_hold" not in stored
    fresh = await db_session.get(Task, task.id, populate_existing=True)
    assert fresh is not None and fresh.payload == {"message": MARKER, "note": MARKER}
