import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, ProcessInstance, Task, User
from coreman.core.db.session import make_session_factory
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.service import WorkerService


class RecordingHandler:
    kind = "chat"

    def __init__(self, hold: float = 0.0) -> None:
        self.hold, self.started, self.cancelled = hold, [], []

    async def run(self, ctx: TaskContext) -> None:
        self.started.append(ctx.task.id)
        deadline = asyncio.get_running_loop().time() + self.hold
        while asyncio.get_running_loop().time() < deadline:
            if ctx.cancel_event.is_set():
                self.cancelled.append((ctx.task.id, ctx.cancel_reason))
                async with ctx.session_factory() as s:
                    await tasks.finish(
                        s, ctx.task.id, status="cancelled", error_code=ctx.cancel_reason
                    )
                    await s.commit()
                return
            await asyncio.sleep(0.05)
        async with ctx.session_factory() as s:
            await tasks.finish(s, ctx.task.id, status="succeeded", result={"ok": True})
            await s.commit()


async def _bot(session: AsyncSession) -> Bot:
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    # bot_key 有 ck_bots_bot_key 约束（至少两个字符），单个 "b" 进不去。
    b = Bot(
        bot_key="b1",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(b)
    await session.commit()
    return b


async def _run_service(service: WorkerService) -> asyncio.Task[None]:
    t = asyncio.create_task(service.run())
    for _ in range(100):
        if service.ready:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError("worker 未就绪")


async def test_claims_via_notify_respects_slots_and_cancels(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    handler = RecordingHandler(hold=1.0)
    service = WorkerService(
        port=0, handlers={"chat": handler}, max_concurrent_override=2, fast_slots_override=1
    )
    runner = await _run_service(service)
    try:
        inst = (await db_session.execute(select(ProcessInstance))).scalar_one()
        assert inst.service == "worker" and inst.capacity == 2
        ids = []
        for i in range(3):
            t = await tasks.enqueue(
                db_session,
                NewTask(bot_id=bot.id, kind="chat", payload={"i": i}, session_key=str(i)),
            )
            assert t
            ids.append(t.id)
        await db_session.commit()
        await asyncio.sleep(0.5)
        assert sorted(handler.started) == ids[:1]  # normal 槽 = 2 - 1 = 1
        assert await tasks.request_cancel(db_session, ids[0], "user_stop") is True
        await db_session.commit()
        await asyncio.sleep(0.5)
        assert handler.cancelled and handler.cancelled[0] == (ids[0], "user_stop")
        await asyncio.sleep(1.5)
        assert sorted(handler.started) == ids
        fast = await tasks.enqueue(
            db_session,
            NewTask(bot_id=bot.id, kind="chat", lane="fast", payload={}, session_key="f"),
        )
        await db_session.commit()
        await asyncio.sleep(0.5)
        assert fast and fast.id in handler.started
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 10)
    async with make_session_factory(db_engine)() as s:
        inst = (await s.execute(select(ProcessInstance))).scalar_one()
        assert inst.stopped_at is not None
        rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
        assert [r.status for r in rows] == ["cancelled", "succeeded", "succeeded", "succeeded"]


async def test_drain_stops_claiming_and_waits(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    handler = RecordingHandler(hold=0.8)
    service = WorkerService(port=0, handlers={"chat": handler}, heartbeat_seconds=0.2)
    runner = await _run_service(service)
    try:
        running = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
        )
        await db_session.commit()
        await asyncio.sleep(0.4)
        assert running and handler.started == [running.id]
        assert await instances.request_drain(db_session, service.instance_id) is True
        await db_session.commit()
        await asyncio.sleep(0.5)
        later = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="b")
        )
        await db_session.commit()
        await asyncio.wait_for(runner, 10)  # 排空后自行退出
        assert later and later.id not in handler.started
    finally:
        if not runner.done():
            service.request_stop("test")
            await asyncio.wait_for(runner, 10)
    async with make_session_factory(db_engine)() as s:
        row = await tasks.get(s, running.id)
        assert row and row.status == "succeeded"
        pending = await tasks.get(s, later.id)
        assert pending and pending.status == "queued"


async def test_unknown_kind_and_handler_crash_are_recorded(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)

    class Boom:
        kind = "chat"

        async def run(self, ctx: TaskContext) -> None:
            raise RuntimeError("boom")

    service = WorkerService(port=0, handlers={"chat": Boom()})
    runner = await _run_service(service)
    try:
        a = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
        )
        b = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="cron_run", payload={}, session_key="b")
        )
        await db_session.commit()
        await asyncio.sleep(0.8)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 10)
    async with make_session_factory(db_engine)() as s:
        assert a and b
        ra, rb = await tasks.get(s, a.id), await tasks.get(s, b.id)
        assert (
            ra
            and ra.status == "failed"
            and ra.error_code == "RuntimeError"
            and "boom" in (ra.error_message or "")
        )
        assert rb and rb.status == "failed" and rb.error_code == "unknown_kind"
        assert datetime.now(UTC) > ra.finished_at
