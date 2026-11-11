"""按行封存与还原：库里是密文，取回是明文；普通机器人不变；销毁后置空不抛错。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.session import SealedSession
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantKey, Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import (
    MASTER_KEY,
    SECRET,
    raw,
    seed_assistant,
    seed_standard,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
PAYLOAD = {"message": {"text": SECRET}, "platform_user_id": "ou_1", "stale_hold": True}


async def _insert(session: AsyncSession, bot_id, payload: dict) -> int:  # type: ignore[no-untyped-def]
    values = await sealed.seal_values(
        session, Task, bot_id, {"bot_id": bot_id, "kind": "chat", "payload": payload}
    )
    task_id = (await session.execute(insert(Task).values(**values).returning(Task.id))).scalar_one()
    await session.commit()
    return int(task_id)


async def _stored(session: AsyncSession, task_id: int) -> str:
    return str(await raw(session, "SELECT payload::text FROM tasks WHERE id = :id", id=task_id))


def test_factory_builds_sealed_sessions(db_session: AsyncSession) -> None:
    assert isinstance(db_session, SealedSession)


async def test_assistant_row_is_ciphertext_at_rest_and_plain_when_read(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    stored = json.loads(await _stored(db_session, task_id))
    assert SECRET not in json.dumps(stored, ensure_ascii=False)
    assert stored["platform_user_id"] == "ou_1" and stored["stale_hold"] is True
    assert stored["_sealed"].startswith("enc:v2:d1:")
    # 首次写入时生成了所有者的密钥。
    assert (await db_session.execute(select(AssistantKey.user_id))).scalar_one() == owner.id
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None and got.payload == PAYLOAD
        assert sealed.is_opened(got) and not sealed.is_locked(got) and got not in other.dirty
        by_scalar = await other.scalar(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )
        assert by_scalar is got and got.payload == PAYLOAD
        await other.refresh(got)
        assert got.payload == PAYLOAD


async def test_standard_bot_row_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    stored = await _stored(db_session, task_id)
    assert SECRET in stored and "_sealed" not in stored
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == PAYLOAD and not sealed.is_opened(got)
    assert (await db_session.execute(select(AssistantKey))).first() is None


async def test_orm_mutation_is_resealed_on_flush(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    task = await db_session.get(Task, task_id, populate_existing=True)
    assert task is not None
    task.payload = {**task.payload, "note": f"{SECRET}-2", "busy_offer": True}
    await db_session.commit()
    # 内存里仍是明文，库里是新的密文，明文键留在外面。
    assert task.payload["note"] == f"{SECRET}-2" and task not in db_session.dirty
    stored = json.loads(await _stored(db_session, task_id))
    assert SECRET not in json.dumps(stored, ensure_ascii=False) and stored["busy_offer"] is True
    async with make_session_factory(db_engine)() as other:
        got = await other.get(Task, task_id)
        assert got is not None
        assert got.payload == {**PAYLOAD, "note": f"{SECRET}-2", "busy_offer": True}


async def test_plain_key_added_by_raw_sql_survives(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    # 与 worker/chat/feishu_docs.py 里那条语句同形。
    await db_session.execute(
        text("UPDATE tasks SET payload = payload || CAST(:extra AS jsonb) WHERE id = :id"),
        {"extra": json.dumps({"doc_links": ["doc_x"]}), "id": task_id},
    )
    await db_session.commit()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == {**PAYLOAD, "doc_links": ["doc_x"]}


async def test_open_value_for_a_single_column(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    column = (await db_session.execute(select(Task.payload).where(Task.id == task_id))).scalar_one()
    assert "message" not in column
    assert await sealed.open_value(db_session, Task, "payload", bot.id, column) == PAYLOAD


async def test_only_the_owner_may_open_when_a_viewer_is_given(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other = (await seed_standard(db_session)).created_by
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    row = (
        await db_session.execute(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert await sealed.open_row(db_session, row, viewer=other) is False
    assert sealed.is_locked(row) and "message" not in row.payload and "_sealed" not in row.payload
    # 已经还原过的行，换成别人看也要收回去。
    mine = await db_session.get(Task, task_id, populate_existing=True)
    assert mine is not None and mine.payload == PAYLOAD
    assert await sealed.open_row(db_session, mine, viewer=other) is False
    assert "message" not in mine.payload
    again = await db_session.get(Task, task_id, populate_existing=True)
    assert await sealed.open_row(db_session, again, viewer=owner.id) is True
    assert again is not None and again.payload == PAYLOAD


async def test_destroyed_key_blanks_reads_and_drops_writes(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and sealed.is_locked(got)
    assert got.payload == {"platform_user_id": "ou_1", "stale_hold": True}
    # 库里的密文没有被改写。
    assert "enc:v2:d1:" in await _stored(db_session, task_id)
    # 销毁之后的写入：内容丢弃，行照常落库，留下墓碑。
    late = await _insert(db_session, bot.id, {"message": SECRET, "stale_hold": True})
    stored = json.loads(await _stored(db_session, late))
    assert stored == {"_sealed": "", "stale_hold": True}
    tomb = await db_session.get(Task, late, populate_existing=True)
    assert tomb is not None and sealed.is_locked(tomb) and tomb.payload == {"stale_hold": True}


async def test_master_key_rotation_keeps_rows_readable(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _insert(db_session, bot.id, PAYLOAD)
    before = await _stored(db_session, task_id)
    sealed.use_master(Cipher(b"\x08" * 32, key_id="k2", previous={"k1": MASTER_KEY}))
    keys.forget()
    got = await db_session.get(Task, task_id, populate_existing=True)
    assert got is not None and got.payload == PAYLOAD
    # 轮换不重写内容。
    assert await _stored(db_session, task_id) == before


async def test_reissued_key_locks_old_rows_and_reads_new_ones(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    old_id = await _insert(db_session, bot.id, PAYLOAD)
    assert '"_sealed": "enc:v2:d1:' in await _stored(db_session, old_id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    # 重新开通：显式签发第 2 代。
    assert await keys.ensure_key(db_session, sealed.master(), owner.id) is True
    await db_session.commit()
    new_id = await _insert(db_session, bot.id, PAYLOAD)
    assert '"_sealed": "enc:v2:d2:' in await _stored(db_session, new_id)
    new = await db_session.get(Task, new_id, populate_existing=True)
    assert new is not None and new.payload == PAYLOAD and not sealed.is_locked(new)
    # 旧一代的行：不抛错，按已锁定处理，库里的旧密文原样留着。
    old = await db_session.get(Task, old_id, populate_existing=True)
    assert old is not None and sealed.is_locked(old)
    assert old.payload == {"platform_user_id": "ou_1", "stale_hold": True}
    assert '"_sealed": "enc:v2:d1:' in await _stored(db_session, old_id)
    column = (await db_session.execute(select(Task.payload).where(Task.id == old_id))).scalar_one()
    assert await sealed.open_value(db_session, Task, "payload", bot.id, column) == {
        "platform_user_id": "ou_1",
        "stale_hold": True,
    }
