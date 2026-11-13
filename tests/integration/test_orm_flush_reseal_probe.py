"""flush 钩子做法的最小验证（AsyncSession + asyncpg + JSONB）。

`coreman/core/assistants/sealed.py` 用同一套做法：读出后把明文作为「已提交的值」放进属性，
落库前换成密文，落库后换回明文。这里用一个不加密的替身变换验证 ORM 的行为本身。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from coreman.core.db.models import Bot, Task, User
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import raw

PROBE = "flush_probe"


def _wrap(value: dict[str, Any]) -> dict[str, Any]:
    """替身变换：库里存的形式。"""
    return {"wrapped": json.dumps(value, sort_keys=True)}


@pytest.fixture
def swap_hooks() -> Iterator[None]:
    swapped: list[tuple[Any, dict[str, Any]]] = []

    def before(session: Session, flush_context: Any, instances: Any) -> None:
        for row in session.dirty:
            if not isinstance(row, Task):
                continue
            state = sa_inspect(row)
            if PROBE in state.info and state.attrs.payload.history.has_changes():
                plain = row.payload
                row.payload = _wrap(plain)
                swapped.append((row, plain))

    def after(session: Session, flush_context: Any) -> None:
        while swapped:
            row, plain = swapped.pop()
            set_committed_value(row, "payload", plain)

    event.listen(Session, "before_flush", before)
    event.listen(Session, "after_flush_postexec", after)
    yield
    event.remove(Session, "before_flush", before)
    event.remove(Session, "after_flush_postexec", after)


async def _stored(session: AsyncSession, task_id: int) -> dict[str, Any]:
    text_ = await raw(session, "SELECT payload::text FROM tasks WHERE id = :id", id=task_id)
    return dict(json.loads(text_))


@pytest.mark.usefixtures("swap_hooks")
async def test_swap_on_flush_and_restore_after(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    creator = User(login_name="probe", display_name="李四")
    db_session.add(creator)
    await db_session.flush()
    bot = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(bot)
    await db_session.flush()
    task_id = int(
        (
            await db_session.execute(
                insert(Task)
                .values(bot_id=bot.id, kind="chat", payload=_wrap({"a": 1}))
                .returning(Task.id)
            )
        ).scalar_one()
    )
    await db_session.commit()

    # 1. 读出后把明文作为已提交的值放进去：不标脏，别的列的更新不会把它写回库。
    row = await db_session.get(Task, task_id)
    assert row is not None
    set_committed_value(row, "payload", {"a": 1})
    sa_inspect(row).info[PROBE] = True
    assert row not in db_session.dirty
    row.priority = 3
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1})

    # 2. 改了内容：落库的是换过的形式，内存里仍是明文，且不再是脏的。
    row.payload = {**row.payload, "b": 2}
    await db_session.flush()
    assert row.payload == {"a": 1, "b": 2} and row not in db_session.dirty
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1, "b": 2})
    assert row.payload == {"a": 1, "b": 2}

    # 3. 由 Core 语句触发的自动 flush 走同一条路。
    row.payload = {**row.payload, "c": 3}
    await db_session.execute(update(Task).where(Task.id == task_id).values(priority=4))
    assert row.payload == {"a": 1, "b": 2, "c": 3} and row not in db_session.dirty
    await db_session.commit()
    assert await _stored(db_session, task_id) == _wrap({"a": 1, "b": 2, "c": 3})

    # 4. 别的会话读到的是库里的形式。
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None and got.payload == _wrap({"a": 1, "b": 2, "c": 3})
