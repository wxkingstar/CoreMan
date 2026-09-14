"""用真实租约和队列、模拟平台验证双实例升级期间的在途工作。"""

import asyncio

from sqlalchemy import func, select

from coreman.core.db.models import InboundEvent, Task
from coreman.core.db.session import make_session_factory
from coreman.operations import drain
from coreman.runtime.bus import tasks
from coreman.runtime.bus.tasks import NewTask
from coreman.runtime.gateway_wecom.service import GatewayWecomService
from coreman.runtime.gateway_wecom.ws_client import WsConfig
from coreman.runtime.worker.service import WorkerService
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.integration.test_gateway_wecom import FAST_WS, _bot, _wait
from tests.integration.test_worker_service import RecordingHandler, _run_service


async def test_gateway_ab_handoff_retains_messages_and_dedup(
    db_engine, db_session, runtime_settings
):
    await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    services, runners = [], []
    factory = make_session_factory(db_engine)
    try:
        for side in ("a", "b"):
            service = GatewayWecomService(
                port=0,
                lease_interval=0.2,
                heartbeat_seconds=0.3,
                poll_interval=0.2,
                ws_config=WsConfig(url=url, **FAST_WS),
                stop_grace_seconds=10,
            )
            service.instance_id = f"gateway-wecom-{side}:node{side}:1"
            services.append(service)
            runners.append(asyncio.create_task(service.run()))
            await _wait(lambda service=service: service.ready)
            if side == "a":
                await _wait(
                    lambda service=service: "bot1" in fake.connections and bool(service.runners)
                )
        await fake.send_message("bot1", text="before", msgid="m-before")

        async def has_before():
            async with factory() as session:
                return await session.scalar(select(func.count()).select_from(InboundEvent)) == 1

        await _wait(has_before)
        await drain(factory, "gateway-wecom-a:nodea:", 15, "gateway-wecom-b:nodeb:")
        await fake.send_message("bot1", text="redelivery", msgid="m-before")
        await fake.send_message("bot1", text="after", msgid="m-after")

        async def has_after():
            async with factory() as session:
                return await session.scalar(select(func.count()).select_from(InboundEvent)) == 2

        await _wait(has_after)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 2
        assert runners[0].done() and not runners[1].done()
    finally:
        for service in services:
            service.request_stop("test")
        await asyncio.wait_for(asyncio.gather(*runners), 20)
        await fake.stop()


async def test_worker_blue_green_finishes_old_and_accepts_new(
    db_engine, db_session, runtime_settings
):
    bot = await _bot(db_session)
    old_handler, new_handler = RecordingHandler(hold=2), RecordingHandler()
    old = WorkerService(port=0, handlers={"chat": old_handler}, heartbeat_seconds=0.1)
    new = WorkerService(port=0, handlers={"chat": new_handler}, heartbeat_seconds=0.1)
    old.instance_id, new.instance_id = "worker-a:old:1", "worker-b:new:1"
    runners = [await _run_service(old)]
    factory = make_session_factory(db_engine)
    try:
        first = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", session_key="first")
        )
        await db_session.commit()
        await _wait(lambda: first.id in old_handler.started)
        runners.append(await _run_service(new))
        handoff = asyncio.create_task(drain(factory, "worker-a:old:", 10, "worker-b:new:"))
        await _wait(lambda: old.draining)
        second = await tasks.enqueue(
            db_session, NewTask(bot_id=bot.id, kind="chat", session_key="second")
        )
        await db_session.commit()
        await handoff

        async def second_completed():
            async with factory() as session:
                row = await session.get(Task, second.id)
                return row is not None and row.status == "succeeded"

        await _wait(second_completed)
        assert second.id in new_handler.started
        async with factory() as session:
            first_row = await session.get(Task, first.id)
            assert first_row.status == "succeeded" and not old_handler.cancelled
        assert second.id not in old_handler.started
    finally:
        old.request_stop("test")
        new.request_stop("test")
        await asyncio.wait_for(asyncio.gather(*runners), 15)


async def test_two_worker_burst_completes_each_task_once(db_engine, db_session, runtime_settings):
    bot = await _bot(db_session)
    services, runners = [], []
    factory = make_session_factory(db_engine)
    try:
        for side in ("a", "b"):
            service = WorkerService(
                port=0, handlers={"chat": RecordingHandler(hold=0.1)}, heartbeat_seconds=0.2
            )
            service.instance_id = f"worker-{side}:burst:1"
            services.append(service)
            runners.append(await _run_service(service))
        for index in range(40):
            await tasks.enqueue(
                db_session, NewTask(bot_id=bot.id, kind="chat", session_key=f"burst-{index}")
            )
        await db_session.commit()

        async def completed():
            async with factory() as session:
                return (
                    await session.scalar(
                        select(func.count()).select_from(Task).where(Task.status == "succeeded")
                    )
                    == 40
                )

        await _wait(completed, timeout=30)
        async with factory() as session:
            rows = list(await session.scalars(select(Task)))
            assert all(row.attempts == 1 for row in rows)
            assert {row.claimed_by for row in rows} <= {service.instance_id for service in services}
    finally:
        for service in services:
            service.request_stop("test")
        await asyncio.wait_for(asyncio.gather(*runners), 15)
