import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat import interactions
from coreman.core.db.models import (
    Bot,
    BotLease,
    ChatLog,
    ChatSession,
    OutboxItem,
    ProcessInstance,
    Task,
    TaskStream,
    User,
)
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.core.settings_store import SettingsStore
from coreman.runtime.bus import instances, leases, outbox, streams, tasks
from coreman.runtime.bus.tasks import NewTask
from coreman.runtime.scheduler import reaper
from coreman.runtime.scheduler.service import SchedulerService


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


async def test_reap_lost_task_completes_stream_and_logs(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _bot(db_session)
    inst = await instances.register(
        db_session, instance_id="w1", service="worker", version="dev", capacity=1
    )
    t = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={
                "message": {"chat_id": "zs", "chat_type": "single"},
                "bot_key": "b",
                "platform_user_id": "zs",
            },
            session_key="zs",
        ),
    )
    assert t
    await db_session.commit()
    assert await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    await tasks.start(db_session, t.id)
    await streams.create(
        db_session,
        task_id=t.id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="s",
        reply_context={"req_id": "r"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, t.id, pending_text="做到一半")
    await db_session.commit()
    now = datetime.now(UTC)
    assert await reaper.reap_lost_tasks(db_session, now) == 0  # 心跳还新鲜
    row = await tasks.get(db_session, t.id)
    assert row
    row.heartbeat_at = now - timedelta(seconds=61)
    await db_session.commit()
    assert (
        await reaper.reap_lost_tasks(
            db_session, now, chat_logs_factory=make_session_factory(db_engine)
        )
        == 1
    )
    await db_session.commit()
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "failed" and row.error_code == "worker_lost"
    s = await streams.get(db_session, t.id)
    assert s and s.is_complete and s.final_text == "做到一半\n\n" + msg("worker_lost")
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.status == "timeout" and log.error_code == "worker_lost" and log.task_id == t.id


async def test_reaper_cleans_only_the_submitted_state_of_the_lost_task(db_session):
    from coreman.core.chat import interactions
    from coreman.core.db.models import InteractionState

    bot = await _bot(db_session)
    state = await interactions.open_state(
        db_session, bot_id=bot.id, kind="choice", scope_key="scope", state={}
    )
    await interactions.set_status(db_session, state.id, "submitted")
    task = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="choice_submit",
            session_key="zs",
            payload={"state_id": str(state.id)},
        ),
    )
    assert task
    task.status = "running"
    task.claimed_at = task.heartbeat_at = datetime.now(UTC) - timedelta(seconds=100)
    await db_session.commit()
    assert await reaper.reap_lost_tasks(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []


async def test_reap_lost_task_falls_back_to_outbox_when_stream_unpushable(
    db_session: AsyncSession,
) -> None:
    """proactive 的流网关不会再推：收尸得改走出站箱，否则用户永远收不到「进程没了」。"""
    bot = await _bot(db_session)
    inst = await instances.register(
        db_session, instance_id="w1", service="worker", version="dev", capacity=1
    )
    t = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": {"chat_id": "群1", "chat_type": "group"}},
            session_key="群1",
        ),
    )
    assert t
    await db_session.commit()
    assert await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    await tasks.start(db_session, t.id)
    await streams.create(
        db_session,
        task_id=t.id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="s",
        reply_context={},
        lease_generation=1,
        running_since=datetime.now(UTC),
        delivery_mode="proactive",
    )
    await streams.update(db_session, t.id, pending_text="做到一半")
    now = datetime.now(UTC)
    row = await tasks.get(db_session, t.id)
    assert row
    row.heartbeat_at = now - timedelta(seconds=61)
    await db_session.commit()
    assert await reaper.reap_lost_tasks(db_session, now) == 1
    await db_session.commit()
    s = await streams.get(db_session, t.id)
    # 推不出去的流不带半截正文：用户只会从出站箱那条消息里看到结果。
    assert s and s.is_complete and s.final_text == msg("worker_lost")
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert item.kind == "send" and item.platform == "wecom"
    assert item.dedupe_key == f"{t.id}:send:lost"
    assert item.target == {"chat_id": "群1"} and item.payload == {"markdown": msg("worker_lost")}
    # 幂等：任务已经不是 running 了，再收一次不会多发一条。
    assert await reaper.reap_lost_tasks(db_session, now) == 0


async def test_reap_also_collects_claimed_tasks(db_session: AsyncSession) -> None:
    """认领了却没来得及 start 的行同样要收：没人再动它，只看 running 就漏了。"""
    bot = await _bot(db_session)
    inst = await instances.register(
        db_session, instance_id="w1", service="worker", version="dev", capacity=1
    )
    t = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": {"chat_id": "zs", "chat_type": "single"}},
            session_key="zs",
        ),
    )
    assert t
    await db_session.commit()
    claimed = await tasks.claim(db_session, lane="normal", instance_id=inst.id)
    assert claimed and claimed.status == "claimed"
    now = datetime.now(UTC)
    row = await tasks.get(db_session, t.id)
    assert row and row.heartbeat_at is not None
    # 认领时刻就很久以前了：heartbeat_at 缺失时用 claimed_at，这里两者都做旧。
    row.heartbeat_at, row.claimed_at = None, now - timedelta(seconds=61)
    await db_session.commit()
    assert await reaper.reap_lost_tasks(db_session, now) == 1
    await db_session.commit()
    done = await tasks.get(db_session, t.id)
    assert done and done.status == "failed" and done.error_code == reaper.LOST_ERROR_CODE
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert item.dedupe_key == f"{t.id}:send:lost" and item.target == {"chat_id": "zs"}


async def test_cleanups(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    now = datetime.now(UTC)
    old, fresh = now - timedelta(hours=2), now
    t1 = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="a")
    )
    t2 = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="b")
    )
    assert t1 and t2
    for t, when in ((t1, old), (t2, fresh)):
        await streams.create(
            db_session,
            task_id=t.id,
            bot_id=bot.id,
            platform="wecom",
            stream_id=f"s{t.id}",
            reply_context={},
            lease_generation=1,
            running_since=when,
        )
        await streams.complete(db_session, t.id, final_text="x")
    await db_session.commit()
    s1 = await streams.get(db_session, t1.id)
    assert s1
    s1.completed_at = old
    a = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key="a",
        target={},
        payload={},
    )
    assert a
    await outbox.mark_sent(db_session, a.id)
    db_session.add(
        ChatSession(
            bot_id=bot.id,
            session_key="k",
            relay_session_id=__import__("uuid").uuid4(),
            backend="claude",
            last_active_at=now - timedelta(hours=80),
        )
    )
    inst = await instances.register(
        db_session, instance_id="gw-x", service="gateway-wecom", version="dev", capacity=None
    )
    await leases.ensure_rows(db_session, "wecom")
    assert await leases.acquire(db_session, bot_id=bot.id, platform="wecom", instance_id=inst.id)
    await db_session.commit()
    ob = (await db_session.execute(select(OutboxItem))).scalar_one()
    ob.sent_at = now - timedelta(days=8)
    lease = await db_session.get(BotLease, bot.id)
    assert lease
    lease.heartbeat_at = now - timedelta(seconds=31)
    inst_row = await db_session.get(ProcessInstance, inst.id)
    assert inst_row
    inst_row.heartbeat_at = now - timedelta(seconds=61)
    await db_session.commit()
    assert await reaper.cleanup_streams(db_session, now) == 1
    assert await reaper.cleanup_outbox(db_session, now) == 1
    assert await reaper.cleanup_sessions(db_session, now, ttl_hours=72) == 1
    assert await reaper.release_stale_leases(db_session, now) == 1
    assert await reaper.mark_dead_instances(db_session, now) == 1
    await db_session.commit()
    assert [s.task_id for s in (await db_session.execute(select(TaskStream))).scalars()] == [t2.id]
    lease = await db_session.get(BotLease, bot.id, populate_existing=True)
    assert lease and lease.holder_instance is None
    inst_row = await db_session.get(ProcessInstance, inst.id, populate_existing=True)
    assert inst_row and inst_row.stopped_at is not None
    inst_row.stopped_at = now - timedelta(days=8)
    await db_session.commit()
    assert await reaper.purge_instances(db_session, now) == 1


async def test_cleanup_expires_interaction_states(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _bot(db_session)
    now = datetime.now(UTC)
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key="s",
        state={},
        expires_at=now - timedelta(minutes=1),
    )
    await interactions.open_state(db_session, bot_id=bot.id, kind="choice", scope_key="c", state={})
    await db_session.commit()
    counts = await reaper.run_cleanup(
        make_session_factory(db_engine), SettingsStore(make_session_factory(db_engine)), now
    )
    assert counts.get("interactions_expired") == 1


async def test_leader_lock_single_leader(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    a = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    b = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    ta, tb = asyncio.create_task(a.run()), asyncio.create_task(b.run())
    try:
        for _ in range(60):
            if a.ready and b.ready:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        assert a.is_leader != b.is_leader
        leader, follower = (a, b) if a.is_leader else (b, a)
        leader.request_stop("test")
        await asyncio.wait_for(ta if leader is a else tb, 10)
        for _ in range(60):
            if follower.is_leader:
                break
            await asyncio.sleep(0.05)
        assert follower.is_leader
    finally:
        for svc, t in ((a, ta), (b, tb)):
            if not t.done():
                svc.request_stop("test")
                await asyncio.wait_for(t, 10)
    async with make_session_factory(db_engine)() as s:
        rows = (await s.execute(select(ProcessInstance))).scalars().all()
        assert {r.service for r in rows} == {"scheduler"} and all(r.stopped_at for r in rows)
        assert (await s.execute(select(Task))).scalars().all() == []


async def test_terminal_task_incomplete_stream_is_recovered_once(db_session):
    bot = await _bot(db_session)
    now = datetime.now(UTC)
    task = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat", session_key="zs"))
    stream = await streams.create(
        db_session,
        task_id=task.id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="recover",
        reply_context={"chat_id": "zs"},
        lease_generation=1,
        running_since=now,
    )
    await streams.update(db_session, task.id, pending_text="已保存的结果")
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    assert await reaper.recover_terminal_streams(db_session, now) == 0
    assert await reaper.recover_terminal_streams(db_session, now + timedelta(seconds=90)) == 1
    await db_session.commit()
    await db_session.refresh(stream)
    assert stream.is_complete and "已保存的结果" in stream.final_text
    assert await reaper.recover_terminal_streams(db_session, now + timedelta(seconds=120)) == 0
    await db_session.refresh(task)
    assert task.status == "succeeded"
