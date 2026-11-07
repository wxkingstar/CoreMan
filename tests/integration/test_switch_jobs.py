"""切换运行时的后台作业：不要求员工空闲、等手上这一轮结束、可取消、中途断掉不让员工停摆。"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots import switch_relay, workspace_transfer
from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, Task, User
from coreman.core.knowledge import memory_transfer
from coreman.core.timeutils import utcnow
from coreman.runtime.scheduler.reaper import reap_lost_tasks
from coreman.runtime.worker.relay_switch import RelaySwitchHandler
from tests.fakes.runtime_node import add_node_relay
from tests.integration.worker_helpers import build_ctx, seed_bot


@pytest.fixture(autouse=True)
def _fast_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_transfer, "DRAIN_POLL_SECONDS", 0.05)


async def _worker(session: AsyncSession, instance_id: str = "w1") -> str:
    await instances.register(
        session, instance_id=instance_id, service="worker", version="test", capacity=4
    )
    await session.commit()
    return instance_id


async def _job(session: AsyncSession, bot: Bot, user: User, target_id: uuid.UUID) -> Task:
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="relay_switch",
            payload={
                "origin": "admin",
                "user_id": str(user.id),
                "target_relay_server_id": str(target_id),
                "workspace_mode": "copy",
            },
        ),
    )
    assert task is not None
    await session.commit()
    return task


async def test_switch_job_waits_for_the_running_turn(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """员工手上有一轮在跑：作业先隔离（不再认领新任务），等这一轮结束再迁移，不再 409。"""
    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    user = await db_session.get(User, bot.created_by)
    chat = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    await tasks.claim(db_session, lane="normal", instance_id=await _worker(db_session))
    await db_session.commit()
    seen: list[str] = []

    async def prepare(session, bot_row, target_row, cipher, **kw):  # type: ignore[no-untyped-def]
        # 只验证隔离与等待，文件处理换成假的：走到迁移这一步时，进行中的那一轮必须已经结束。
        assert kw["drain"] is True
        await memory_transfer.drain(session, bot_row, cancelled=kw["cancelled"])
        seen.append((await session.get(Task, chat.id, populate_existing=True)).status)
        return "transferred", True, set()

    monkeypatch.setattr(switch_relay, "prepare_switch", prepare)
    job = await _job(db_session, bot, user, target.id)
    run = asyncio.create_task(RelaySwitchHandler().run(build_ctx(db_engine, job)))
    await asyncio.sleep(0.2)
    assert not run.done()
    async with db_engine.connect() as conn:
        await conn.execute(
            Task.__table__.update().where(Task.id == chat.id).values(status="succeeded")
        )
        await conn.commit()
    await asyncio.wait_for(run, 5)
    assert seen == ["succeeded"]
    await db_session.refresh(bot)
    assert bot.relay_server_id == target.id
    finished = await db_session.get(Task, job.id, populate_existing=True)
    assert finished.status == "succeeded" and finished.result["ok"] is True


async def test_drain_fences_new_claims_and_can_be_cancelled(
    db_engine: AsyncEngine, db_session: AsyncSession
):
    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    await tasks.claim(db_session, lane="normal", instance_id=await _worker(db_session))
    queued = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    cancelled = asyncio.Event()
    run = asyncio.create_task(
        workspace_transfer.prepare_switch(
            db_session,
            bot,
            target,
            None,
            directory=bot.working_dir,
            drain=True,
            cancelled=cancelled,
        )
    )
    await asyncio.sleep(0.2)
    # 隔离期间排队的消息不会被认领。
    async with db_engine.connect() as conn:
        async with AsyncSession(conn) as other:
            assert await tasks.claim(other, lane="normal", instance_id="w1") is None
    cancelled.set()
    with pytest.raises(Exception, match="切换已取消"):
        await asyncio.wait_for(run, 5)
    await db_session.refresh(bot)
    assert bot.workspace_state == "ready" and bot.relay_server_id == old.id
    assert (await db_session.get(Task, queued.id, populate_existing=True)).status == "queued"


async def test_reaper_releases_the_fence_of_a_lost_switch_job(db_session: AsyncSession):
    """作业所在的 worker 没了：员工不能一直停在迁移中，回到 ready，绑定不变。"""
    bot, old, _ = await seed_bot(db_session)
    target = await add_node_relay(db_session, name="target")
    user = await db_session.get(User, bot.created_by)
    job = await _job(db_session, bot, user, target.id)
    await tasks.claim(db_session, lane="normal", instance_id=await _worker(db_session))
    bot.workspace_state, bot.workspace_phase = "migrating", "迁移会话记录"
    bot.workspace_target_relay_id = target.id
    await db_session.execute(
        Task.__table__.update()
        .where(Task.id == job.id)
        .values(heartbeat_at=utcnow().replace(year=2020))
    )
    await db_session.commit()
    assert await reap_lost_tasks(db_session, utcnow()) == 1
    await db_session.commit()
    await db_session.refresh(bot)
    assert bot.workspace_state == "ready" and bot.relay_server_id == old.id
    assert bot.workspace_target_relay_id is None
    assert "切换中断" in (bot.workspace_phase or "")


async def test_switch_job_is_claimed_even_when_workspace_is_not_ready(db_session: AsyncSession):
    """初始化失败的员工只能靠切换离开这个状态：切换作业照样认领，其他任务不认领。"""
    bot, _, _ = await seed_bot(db_session)
    bot.workspace_state = "failed"
    await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat"))
    switch = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="relay_switch"))
    await db_session.commit()
    worker = await _worker(db_session)
    claimed = await tasks.claim(db_session, lane="normal", instance_id=worker)
    assert claimed is not None and claimed.id == switch.id
    assert await tasks.claim(db_session, lane="normal", instance_id=worker) is None
