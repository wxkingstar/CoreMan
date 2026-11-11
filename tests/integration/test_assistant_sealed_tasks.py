"""tasks：载荷与结果在库里是密文，经 bus 函数取回是明文；被 SQL 读取的键仍在明文部分。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
WORKER = "worker-a:h:1:1"


async def _worker(session: AsyncSession) -> None:
    await instances.register(
        session, instance_id=WORKER, service="worker", version="dev", capacity=30
    )
    await session.commit()


def _chat(bot_id, **extra: object) -> NewTask:  # type: ignore[no-untyped-def]
    payload = {"message": {"text": SECRET}, "platform_user_id": "ou_1", **extra}
    return NewTask(bot_id=bot_id, kind="chat", payload=payload, session_key="oc_1")


async def _column(session: AsyncSession, column: str, task_id: int) -> str:
    return str(await raw(session, f"SELECT {column}::text FROM tasks WHERE id = :id", id=task_id))


async def test_payload_and_result_roundtrip(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    await _worker(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    await db_session.commit()
    assert task is not None and task.payload["message"]["text"] == SECRET
    stored = await _column(db_session, "payload", task.id)
    assert SECRET not in stored and json.loads(stored)["platform_user_id"] == "ou_1"
    factory = make_session_factory(db_engine)
    async with factory() as other:
        claimed = await tasks.claim(other, lane="normal", instance_id=WORKER)
        await other.commit()
        assert claimed is not None and claimed.id == task.id
        assert claimed.payload == {"message": {"text": SECRET}, "platform_user_id": "ou_1"}
        active = await tasks.active_for_session(other, bot.id, "oc_1")
        assert [t.payload["message"]["text"] for t in active] == [SECRET]
        opened = await tasks.open_for_session(other, bot.id, "oc_1", "chat")
        assert [t.id for t in opened] == [task.id] and opened[0].payload["message"]
        # 不传 bot_id：按主键查到这条任务属于谁。
        assert await tasks.finish(other, task.id, status="succeeded", result={"reply": SECRET})
        await other.commit()
    assert SECRET not in await _column(db_session, "result", task.id)
    async with factory() as other:
        got = await tasks.get(other, task.id)
        assert got is not None and got.result == {"reply": SECRET}


async def test_claim_sql_still_reads_plain_keys(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    await _worker(db_session)
    first = await tasks.enqueue(db_session, _chat(bot.id))
    await db_session.commit()
    assert first is not None
    assert (await tasks.claim(db_session, lane="normal", instance_id=WORKER)) is not None
    await db_session.commit()
    second = await tasks.enqueue(db_session, _chat(bot.id, serialize_session=True))
    await db_session.commit()
    assert second is not None
    # 认领语句里的 payload->>'serialize_session' 在封装上照样命中：前一条没做完，这条不放行。
    assert await tasks.claim(db_session, lane="normal", instance_id=WORKER) is None
    assert not await tasks.has_newer(db_session, bot.id, "oc_1", task_id=first.id, kind="chat")


async def test_release_hold_removes_the_plain_key(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    held = await tasks.enqueue(db_session, _chat(bot.id, stale_hold=True))
    await db_session.commit()
    assert held is not None
    row = await db_session.get(Task, held.id, with_for_update=True, populate_existing=True)
    assert row is not None
    await tasks.release_hold(db_session, row)
    await db_session.commit()
    stored = json.loads(await _column(db_session, "payload", held.id))
    assert "stale_hold" not in stored and "_sealed" in stored and "stale_confirmed" not in stored
    async with make_session_factory(db_engine)() as other:
        got = await tasks.get(other, held.id)
        assert got is not None and got.payload["stale_confirmed"] is True
        assert got.payload["message"]["text"] == SECRET


async def test_claim_fails_a_task_whose_key_is_destroyed(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await _worker(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert task is not None
    assert await tasks.claim(db_session, lane="normal", instance_id=WORKER) is None
    await db_session.commit()
    assert await _column(db_session, "status", task.id) == "failed"
    assert await _column(db_session, "error_code", task.id) == "content_locked"
    # 销毁之后结束任务：不抛错，结果丢弃。
    assert await tasks.finish(db_session, task.id, status="failed", result={"reply": SECRET})
    await db_session.commit()
    assert json.loads(await _column(db_session, "result", task.id)) == {"_sealed": ""}


async def test_standard_bot_tasks_stay_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task = await tasks.enqueue(db_session, _chat(bot.id))
    assert task is not None
    await tasks.finish(db_session, task.id, status="succeeded", result={"reply": SECRET})
    await db_session.commit()
    assert SECRET in await _column(db_session, "payload", task.id)
    assert "_sealed" not in await _column(db_session, "payload", task.id)
    assert json.loads(await _column(db_session, "result", task.id)) == {"reply": SECRET}
