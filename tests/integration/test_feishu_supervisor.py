import asyncio
import time

from coreman.core.db.models import Bot, BotLease, User
from coreman.runtime.gateway_feishu.service import GatewayFeishuService


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_supervisor_restarts_with_backoff_and_stops_before_releasing(
    db_session, runtime_settings, monkeypatch
):
    user = User(display_name="test")
    db_session.add(user)
    await db_session.flush()
    bot = Bot(
        bot_key="feishu",
        platform="feishu",
        name="test",
        created_by=user.id,
        model="vllm/test",
        working_dir="/d",
        credentials_enc="unused",
    )
    db_session.add(bot)
    await db_session.commit()
    service = GatewayFeishuService(port=0, interval=30)
    processes = []

    async def spawn(bot_id, child):
        assert bot_id == bot.id
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(service, "spawn", spawn)
    await service.on_start()
    try:
        for _ in range(100):
            if service.children and service.children[bot.id].process:
                break
            await asyncio.sleep(0.02)
        assert len(processes) == 1
        child = service.children[bot.id]
        processes[0].returncode = 1
        await service.reconcile()
        assert child.process is None and child.next_start > time.monotonic()
        await service.reconcile()
        assert len(processes) == 1
        child.next_start = 0
        await service.reconcile()
        assert len(processes) == 2
        async with service.factory() as session:
            lease = await session.get(BotLease, bot.id)
            assert lease.holder_instance == service.instance_id
    finally:
        await service.on_shutdown()
    assert processes[-1].terminated
    await db_session.refresh(bot)
    lease = await db_session.get(BotLease, bot.id, populate_existing=True)
    assert lease.holder_instance is None
