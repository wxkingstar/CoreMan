import asyncio
from collections.abc import Callable
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

    def __init__(self, hold: float = 0.0, *, release: asyncio.Event | None = None) -> None:
        # 给了 release 就一直跑到它被 set（hold 仍是下限），用例据此精确决定任务何时结束。
        self.hold, self.release, self.started, self.cancelled = hold, release, [], []
        # 每开始一个任务 set 一次：用例等它，而不是赌固定 sleep 够认领加启动。
        self.task_started = asyncio.Event()

    def _holding(self, deadline: float) -> bool:
        if asyncio.get_running_loop().time() < deadline:
            return True
        return self.release is not None and not self.release.is_set()

    async def run(self, ctx: TaskContext) -> None:
        self.started.append(ctx.task.id)
        self.task_started.set()
        deadline = asyncio.get_running_loop().time() + self.hold
        while self._holding(deadline):
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


async def _until(predicate: Callable[[], bool], failure: str) -> None:
    try:
        async with asyncio.timeout(10):
            while not predicate():  # noqa: ASYNC110 ready / draining 是服务暴露的布尔标志
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(failure) from None


async def _run_service(service: WorkerService) -> asyncio.Task[None]:
    t = asyncio.create_task(service.run())
    await _until(lambda: service.ready, "worker 未就绪")
    return t


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
    release = asyncio.Event()
    handler = RecordingHandler(release=release)
    # 兜底轮询拉长到用例跑不到的时长：认领只由入队通知和任务结束触发，排空生效前后
    # 不会恰好有一轮定时认领在飞。
    service = WorkerService(
        port=0, handlers={"chat": handler}, heartbeat_seconds=0.2, poll_interval=60.0
    )
    runner = await _run_service(service)
    try:
        running = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
        )
        await db_session.commit()
        await asyncio.wait_for(handler.task_started.wait(), 10)
        assert running and handler.started == [running.id]
        assert await instances.request_drain(db_session, service.instance_id) is True
        await db_session.commit()
        await _until(lambda: service.draining, "worker 没有察觉排空请求")
        later = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="b")
        )
        await db_session.commit()
        assert not runner.done()  # 在途任务还没跑完，排空不能先退出
        release.set()
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


async def test_queued_notice_survives_a_claim_in_flight_on_the_other_lane(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """normal 车道的认领语句还没返回（快照早于入队）时，fast 车道先被这次入队叫醒。

    两条车道若直接共用监听器上 tasks_queued 的同一个事件，fast 醒来就把它清掉了，normal 认领
    落空、回去等待时再也看不到这次入队，只能干等兜底轮询。兜底轮询拉长到用例跑不到的时长，
    任务仍须立刻被认领。
    """
    bot = await _bot(db_session)
    real_claim = tasks.claim
    normal_in_flight, release_normal, fast_woke = asyncio.Event(), asyncio.Event(), asyncio.Event()
    queued = False

    async def gated_claim(session: AsyncSession, *, lane: str, instance_id: str) -> Task | None:
        if lane == "fast" and queued:
            fast_woke.set()
        claimed = await real_claim(session, lane=lane, instance_id=instance_id)
        if lane == "normal" and not release_normal.is_set():
            # 已经查过一遍、没看到后面才入队的任务，停在这里模拟语句还没返回。
            normal_in_flight.set()
            await release_normal.wait()
        return claimed

    monkeypatch.setattr(tasks, "claim", gated_claim)
    handler = RecordingHandler()
    service = WorkerService(
        port=0,
        handlers={"chat": handler},
        poll_interval=60.0,
        max_concurrent_override=2,
        fast_slots_override=1,
    )
    runner = await _run_service(service)
    try:
        await asyncio.wait_for(normal_in_flight.wait(), 10)
        task = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
        )
        queued = True  # 通知随提交发出，所以在提交之前置位
        await db_session.commit()
        await asyncio.wait_for(fast_woke.wait(), 10)
        release_normal.set()
        await asyncio.wait_for(handler.task_started.wait(), 10)
        assert task and handler.started == [task.id]
    finally:
        release_normal.set()
        service.request_stop("test")
        await asyncio.wait_for(runner, 10)


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


async def test_handler_crash_after_open_closes_the_running_turn(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    from coreman.core.chat import chat_logs
    from coreman.core.chat.chat_logs import ChatLogEntry
    from coreman.core.db.models import ChatLog

    bot = await _bot(db_session)

    class OpensThenCrashes:
        kind = "chat"

        async def run(self, ctx: TaskContext) -> None:
            async with ctx.session_factory() as session:
                await chat_logs.open_turn(
                    session,
                    ChatLogEntry(
                        bot_id=bot.id,
                        bot_key=bot.bot_key,
                        platform="wecom",
                        chat_type="group",
                        message_type="text",
                        status="running",
                        request_at=datetime.now(UTC),
                        task_id=ctx.task.id,
                        message_content="开流之后崩了",
                    ),
                )
                await session.commit()
            raise RuntimeError("boom")

    service = WorkerService(port=0, handlers={"chat": OpensThenCrashes()})
    runner = await _run_service(service)
    try:
        task = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
        )
        await db_session.commit()
        assert task
        factory = make_session_factory(db_engine)
        for _ in range(100):
            async with factory() as s:
                row = await tasks.get(s, task.id)
                if row and row.status not in tasks.OPEN:
                    break
            await asyncio.sleep(0.05)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 10)
    async with factory() as s:
        log = await s.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert log is not None and log.status == "error" and log.error_code == "RuntimeError"
    assert log.message_content == "开流之后崩了" and log.latency_ms is not None


async def test_heartbeat_loop_is_per_task_and_isolates_failures(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    beats: list[str] = []
    original = TaskContext.heartbeat

    async def flaky(self: TaskContext) -> None:
        beats.append(str(self.task.session_key))
        if self.task.session_key == "a":
            raise RuntimeError("db blip")
        await original(self)

    monkeypatch.setattr(TaskContext, "heartbeat", flaky)
    handler = RecordingHandler(hold=1.2)
    service = WorkerService(
        port=0,
        handlers={"chat": handler},
        heartbeat_seconds=0.2,
        max_concurrent_override=4,
        fast_slots_override=0,
    )
    runner = await _run_service(service)
    try:
        for key in ("a", "b"):
            assert await tasks.enqueue(
                db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key=key)
            )
        await db_session.commit()
        await asyncio.sleep(1.0)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 10)
    # 服务心跳循环是任务心跳的唯一来源：任务 a 每次都写失败，任务 b 的心跳照样每轮都写。
    assert beats.count("a") >= 3 and beats.count("b") >= 3, beats
