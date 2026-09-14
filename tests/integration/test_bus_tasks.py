import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import Bot, ProcessInstance, User
from coreman.core.db.session import make_session_factory
from coreman.runtime.bus import instances, tasks
from coreman.runtime.bus.tasks import NewTask


async def _bot(session: AsyncSession, key: str = "b1") -> Bot:
    user = User(login_name=f"c-{key}", display_name="c")
    session.add(user)
    await session.flush()
    bot = Bot(
        bot_key=key,
        platform="wecom",
        name=key,
        created_by=user.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(bot)
    await session.commit()
    return bot


async def _instance(session: AsyncSession, name: str = "worker-a:h:1:1") -> ProcessInstance:
    inst = await instances.register(
        session, instance_id=name, service="worker", version="dev", capacity=30
    )
    await session.commit()
    return inst


async def test_enqueue_dedupe_and_claim_order(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    inst = await _instance(db_session)
    low = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={"n": 1}, session_key="u1")
    )
    high = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="chat", payload={"n": 2}, priority=5, session_key="u2"),
    )
    fast = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="command", lane="fast", payload={"n": 3}, dedupe_key="k1"),
    )
    assert (
        await tasks.enqueue(
            db_session,
            NewTask(bot_id=bot.id, kind="command", lane="fast", payload={}, dedupe_key="k1"),
        )
        is None
    )
    await db_session.commit()
    assert low and high and fast
    first = await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    await db_session.commit()
    assert (
        first is not None
        and first.id == high.id
        and first.status == "claimed"
        and first.attempts == 1
    )
    assert first.claimed_by == inst.id and first.claimed_at is not None
    second = await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    await db_session.commit()
    assert second is not None and second.id == low.id
    assert await tasks.claim(db_session, lane="normal", instance_id=inst.id) is None
    f = await tasks.claim(db_session, lane="fast", instance_id=inst.id)
    assert f is not None and f.id == fast.id
    await db_session.commit()
    assert await tasks.queue_depth(db_session) == {"normal": 0, "fast": 0}


async def test_claim_is_exclusive_across_sessions(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _bot(db_session)
    inst = await _instance(db_session)
    for i in range(4):
        await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={"i": i}, session_key=str(i))
        )
    await db_session.commit()
    factory = make_session_factory(db_engine)

    async def grab() -> int | None:
        async with factory() as s:
            t = await tasks.claim(s, lane="normal", instance_id=inst.id)
            await asyncio.sleep(0.05)
            await s.commit()
            return t.id if t else None

    ids = await asyncio.gather(*(grab() for _ in range(4)))
    assert (
        sorted(i for i in ids if i) == sorted(set(i for i in ids if i))
        and len([i for i in ids if i]) == 4
    )


async def test_lifecycle_cancel_and_supersede(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    inst = await _instance(db_session)
    old = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u1")
    )
    other = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u2")
    )
    assert old and other
    await db_session.commit()
    claimed = await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    assert claimed and claimed.id == old.id
    await tasks.start(db_session, old.id)
    await db_session.commit()
    active = await tasks.active_for_session(db_session, bot.id, "u1")
    assert [t.id for t in active] == [old.id] and active[0].status == "running"
    assert await tasks.heartbeat(db_session, old.id) == (tasks.Heartbeat.OK, None)
    new = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u1")
    )
    assert new
    assert await tasks.supersede(db_session, bot.id, "u1", except_task_id=new.id) == [old.id]
    await db_session.commit()
    assert await tasks.heartbeat(db_session, old.id) == (tasks.Heartbeat.CANCELLED, "superseded")
    assert (
        await tasks.finish(
            db_session, old.id, status="cancelled", error_code="superseded", only_active=True
        )
        is True
    )
    await db_session.commit()
    row = await tasks.get(db_session, old.id)
    assert (
        row
        and row.status == "cancelled"
        and row.finished_at is not None
        and row.error_code == "superseded"
    )
    # 行已不在 ACTIVE（这里是被 supersede 结掉的，线上多半是 reaper 收的尸）：持有者的心跳
    # 必须听得出差别，收尾的 finish 也要被守卫挡住，绝不能把终态改回去。
    assert await tasks.heartbeat(db_session, old.id) == (tasks.Heartbeat.LOST, None)
    assert await tasks.finish(db_session, old.id, status="succeeded", only_active=True) is False
    await db_session.commit()
    row = await tasks.get(db_session, old.id)
    assert row and row.status == "cancelled" and row.error_code == "superseded"
    assert await tasks.request_cancel(db_session, old.id, "user_stop") is False  # 已结束
    assert await tasks.request_cancel(db_session, other.id, "user_stop") is True
    await db_session.commit()
    o = await tasks.get(db_session, other.id)
    assert o and o.cancel_reason == "user_stop" and o.cancel_requested_at is not None
    await instances.heartbeat(db_session, inst.id, running=1)
    assert await instances.request_drain(db_session, inst.id) is True
    await db_session.commit()
    assert await instances.heartbeat(db_session, inst.id, running=1) is True
    await instances.mark_stopped(db_session, inst.id)
    await db_session.commit()
    inst_row = (await db_session.execute(select(ProcessInstance))).scalar_one()
    assert inst_row.stopped_at is not None and inst_row.running == 1


async def test_list_active_and_unknown_task(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    inst = await _instance(db_session)
    t = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u1")
    )
    await db_session.commit()
    assert t and (await tasks.claim(db_session, lane="normal", instance_id=inst.id)) is not None
    await db_session.commit()
    assert [x.id for x in await tasks.list_active(db_session)] == [t.id]
    assert await tasks.get(db_session, 999999) is None
    assert await tasks.heartbeat(db_session, 999999) == (tasks.Heartbeat.LOST, None)
    assert uuid.UUID(str(bot.id))
