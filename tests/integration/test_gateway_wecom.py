import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.bus import instances, leases, outbox, streams
from coreman.core.bus import tasks as tasks_module
from coreman.core.bus.notify import notify
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotLease,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
    User,
)
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.gateway_wecom.service import GatewayWecomService
from coreman.runtime.gateway_wecom.ws_client import WsConfig
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.integration.worker_helpers import MASTER

FAST_WS = dict(
    ping_interval=0.5,
    watchdog_interval=0.2,
    idle_timeout=5.0,
    reconnect_base=0.05,
    reconnect_max=0.2,
    subscribe_timeout=1.0,
)


async def _bot(session: AsyncSession, *, welcome: str | None = None) -> Bot:
    cipher = Cipher(MASTER)
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    b = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="小助手",
        created_by=u.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        welcome_message=welcome,
        credentials_enc=cipher.encrypt(
            json.dumps({"bot_id": "bot1", "secret": "sec"}), CREDENTIALS_AAD
        ),
    )
    session.add(b)
    await session.commit()
    return b


async def _start(url: str) -> tuple[GatewayWecomService, asyncio.Task[None]]:
    service = GatewayWecomService(
        port=0,
        lease_interval=0.2,
        heartbeat_seconds=0.3,
        poll_interval=0.2,
        ws_config=WsConfig(url=url, **FAST_WS),
        stop_grace_seconds=10,
    )
    runner = asyncio.create_task(service.run())
    for _ in range(100):
        if service.ready:
            return service, runner
        await asyncio.sleep(0.05)
    raise AssertionError("网关未就绪")


async def _wait(pred, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]  # noqa: ASYNC109
    for _ in range(int(timeout / 0.05)):
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("条件未满足")


async def test_lease_connect_inbound_dedupe_and_welcome(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session, welcome="欢迎！")
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:

        async def subscribed() -> bool:
            async with make_session_factory(db_engine)() as s:
                lease = await s.get(BotLease, bot.id)
                return bool(
                    lease
                    and lease.holder_instance == service.instance_id
                    and lease.connection_state == "subscribed"
                )

        await _wait(subscribed)
        req = await fake.send_message("bot1", text="你好", msgid="m1")

        async def enqueued() -> bool:
            async with make_session_factory(db_engine)() as s:
                return len((await s.execute(select(Task))).scalars().all()) == 1

        await _wait(enqueued)
        await fake.send_message("bot1", text="你好", msgid="m1")  # 平台重推
        await fake.send_message("bot1", text="stop", msgid="m2")
        await fake.send_event("bot1", "enter_chat", msgid="e1")

        async def state_ok() -> bool:
            async with make_session_factory(db_engine)() as s:
                rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
                events = (await s.execute(select(InboundEvent))).scalars().all()
                ob = (await s.execute(select(OutboxItem))).scalars().all()
                return (
                    len(rows) == 2 and len(events) == 3 and len(ob) == 1 and ob[0].status == "sent"
                )

        await _wait(state_ok)
        async with make_session_factory(db_engine)() as s:
            rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
            assert (
                rows[0].kind == "chat"
                and rows[0].session_key == "zs"
                and rows[0].payload["message"]["reply_context"]["req_id"] == req
            )
            assert (
                rows[1].kind == "command"
                and rows[1].lane == "fast"
                and rows[1].payload["command"] == "stop"
            )
        welcome = await fake.wait_frame(lambda f: f["cmd"] == "aibot_respond_welcome_msg")
        assert welcome["body"]["text"]["content"] == "欢迎！"
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_stream_push_finish_and_outbox(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections)
        req = await fake.send_message("bot1", text="hi", msgid="m1")

        async def got_task() -> bool:
            async with make_session_factory(db_engine)() as s:
                return (await s.execute(select(Task))).scalar_one_or_none() is not None

        await _wait(got_task)
        async with make_session_factory(db_engine)() as s:
            task = (await s.execute(select(Task))).scalar_one()
            lease = await s.get(BotLease, bot.id)
            assert lease
            await streams.create(
                s,
                task_id=task.id,
                bot_id=bot.id,
                platform="wecom",
                stream_id="s1",
                reply_context={"gateway_instance": service.instance_id, "req_id": req},
                lease_generation=lease.generation,
                running_since=datetime.now(UTC),
            )
            await streams.update(s, task.id, thinking_md="🤔 思考")
            await s.commit()
        first = await fake.wait_frame(lambda f: f["cmd"] == "aibot_respond_msg")
        assert first["body"]["stream"] == {
            "id": "s1",
            "finish": False,
            "content": "<think>\n🤔 思考",
        }
        async with make_session_factory(db_engine)() as s:
            for i in range(5):
                await streams.update(s, task.id, pending_text=f"正文{i}")
                await s.commit()
                await asyncio.sleep(0.05)
        await asyncio.sleep(1.5)
        contents = fake.stream_contents(req)
        assert (
            2 <= len(contents) <= 4
            and contents[-1][0].endswith("正文4" + msg("running_indicator"))
            and not contents[-1][1]
        )
        async with make_session_factory(db_engine)() as s:
            await streams.complete(s, task.id, final_text="完成")
            await s.commit()
        await _wait(lambda: fake.stream_contents(req) and fake.stream_contents(req)[-1][1])
        assert fake.stream_contents(req)[-1] == ("<think>\n🤔 思考\n</think>\n\n完成", True)
        async with make_session_factory(db_engine)() as s:
            row = await streams.get(s, task.id)
            assert row and row.finish_pushed_at is not None and row.pushed_version == row.version
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="send",
                dedupe_key="x:send:1",
                target={"chat_id": "zs"},
                payload={"markdown": "主动消息"},
            )
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="send",
                dedupe_key="x:send:2",
                target={"chat_id": "zs"},
                payload={"markdown": "旧代次"},
                lease_generation=999,
            )
            await s.commit()
        sent = await fake.wait_frame(lambda f: f["cmd"] == "aibot_send_msg")
        assert sent["body"] == {
            "chatid": "zs",
            "msgtype": "markdown",
            "markdown": {"content": "主动消息"},
        }

        async def statuses() -> bool:
            async with make_session_factory(db_engine)() as s:
                rows = {
                    r.dedupe_key: r.status for r in (await s.execute(select(OutboxItem))).scalars()
                }
                return rows == {"x:send:1": "sent", "x:send:2": "skipped"}

        await _wait(statuses)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_disable_via_config_changed_and_drain(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections)
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        async with make_session_factory(db_engine)() as s:
            task = None
            for _ in range(50):
                task = (await s.execute(select(Task))).scalar_one_or_none()
                if task:
                    break
                await asyncio.sleep(0.05)
            assert task
            lease = await s.get(BotLease, bot.id)
            assert lease
            await streams.create(
                s,
                task_id=task.id,
                bot_id=bot.id,
                platform="wecom",
                stream_id="s1",
                reply_context={"gateway_instance": service.instance_id, "req_id": req},
                lease_generation=lease.generation,
                running_since=datetime.now(UTC),
            )
            await streams.update(s, task.id, pending_text="进行中")
            await s.commit()
        await _wait(lambda: fake.stream_contents(req))
        async with make_session_factory(db_engine)() as s:
            assert await instances.request_drain(s, service.instance_id)
            await s.commit()
        await asyncio.wait_for(runner, 15)  # 排空后自行退出
        contents = fake.stream_contents(req)
        assert contents[-1][1] is True and contents[-1][0].endswith(msg("drain_suffix"))
        async with make_session_factory(db_engine)() as s:
            row = await streams.get(s, task.id)
            assert row and row.delivery_mode == "proactive" and row.finish_pushed_at is not None
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            assert lease and lease.holder_instance is None and lease.released_at is not None
        await _wait(lambda: "bot1" not in fake.connections)
    finally:
        if not runner.done():
            service.request_stop("test")
            await asyncio.wait_for(runner, 15)
    # 停用 bot → 新实例不再认领（同一个 fake 服务端要留到最后，第二段还得往它上面连）
    service2, runner2 = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections)
        async with make_session_factory(db_engine)() as s:
            b = await s.get(Bot, bot.id)
            assert b
            b.enabled = False
            await notify(s, "config_changed", {"table": "bots", "id": str(bot.id)})
            await s.commit()
        await _wait(lambda: "bot1" not in fake.connections)

        # 关 WS 是排空第 ③ 步，租约提交是第 ④ 步；不能把服务端关帧当作提交屏障。
        async def released_after_disable() -> bool:
            async with make_session_factory(db_engine)() as s:
                lease = await s.get(BotLease, bot.id, populate_existing=True)
                return bool(lease and lease.holder_instance is None)

        await _wait(released_after_disable)
    finally:
        service2.request_stop("test")
        await asyncio.wait_for(runner2, 15)
        await fake.stop()


async def _seed_stream(db_engine: AsyncEngine, bot_id, service: GatewayWecomService, req: str):  # type: ignore[no-untyped-def]
    """等网关把入站消息入队，再按 worker 的做法开一条流。"""
    async with make_session_factory(db_engine)() as s:
        task = None
        for _ in range(100):
            task = (await s.execute(select(Task))).scalar_one_or_none()
            if task:
                break
            await asyncio.sleep(0.05)
        assert task
        lease = await s.get(BotLease, bot_id)
        assert lease
        await streams.create(
            s,
            task_id=task.id,
            bot_id=bot_id,
            platform="wecom",
            stream_id="s1",
            reply_context={"gateway_instance": service.instance_id, "req_id": req},
            lease_generation=lease.generation,
            running_since=datetime.now(UTC),
        )
        await streams.update(s, task.id, pending_text="正文")
        await s.commit()
        return int(task.id)


async def test_proactive_stream_is_finished_once_with_suffix(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """worker 切后台（background_state 契约）→ 网关补一次 finish，之后不再跟。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections)
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)
        await _wait(lambda: fake.stream_contents(req))
        tail = "⏳ 任务耗时较长，仍在后台运行中。"
        async with make_session_factory(db_engine)() as s:
            await streams.update(
                s,
                task_id,
                delivery_mode="proactive",
                background_state={
                    "mode": "incremental",
                    "offset": 2,
                    "finish_suffix": tail,
                    "switched_at": datetime.now(UTC).isoformat(),
                },
            )
            await s.commit()
        await _wait(lambda: fake.stream_contents(req)[-1][1])
        assert fake.stream_contents(req)[-1] == ("正文" + msg("running_indicator") + tail, True)
        pushed = len(fake.stream_contents(req))
        async with make_session_factory(db_engine)() as s:
            await streams.update(s, task_id, pending_text="正文继续")
            await s.commit()
        await asyncio.sleep(0.8)
        assert len(fake.stream_contents(req)) == pushed  # finish 之后同一 req_id 不再推
        async with make_session_factory(db_engine)() as s:
            row = await streams.get(s, task_id)
            assert row and row.finish_pushed_at is not None
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_rate_limited_send_is_deferred_not_failed(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """同一会话连发两条：第二条被会话频控推迟重发（不算失败、不涨 attempts）。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections)
        async with make_session_factory(db_engine)() as s:
            for i in (1, 2):
                await outbox.add(
                    s,
                    bot_id=bot.id,
                    platform="wecom",
                    kind="send",
                    dedupe_key=f"y:send:{i}",
                    target={"chat_id": "zs"},
                    payload={"markdown": f"第{i}条"},
                )
            await s.commit()
        await _wait(lambda: len(fake.sent_messages()) == 2, timeout=8)
        assert [b["markdown"]["content"] for b in fake.sent_messages()] == ["第1条", "第2条"]

        expected = {"y:send:1": ("sent", 0), "y:send:2": ("sent", 0)}

        async def _rows() -> dict[str, tuple[str, int]]:
            async with make_session_factory(db_engine)() as s:
                return {
                    r.dedupe_key: (r.status, r.attempts)
                    for r in (await s.execute(select(OutboxItem))).scalars()
                }

        async def rows_settled() -> bool:
            return await _rows() == expected

        # 假企微收到帧只说明 `ws.send` 返回了：`mark_sent` 与 commit 都排在它后面，
        # 紧跟着读库会看到还是 pending。等库里落定再断言，别拿帧的到达当写库完成。
        await _wait(rows_settled, timeout=8)
        assert await _rows() == expected
        # 企微回 846607：反查 req_id 拿到会话，该会话进入 10 秒退避。
        await fake.wait_frame(lambda f: f["cmd"] == "aibot_send_msg")
        service.runners[bot.id].outbox.note_rate_limited("zs")
        await _wait(lambda: service.runners[bot.id].outbox.wait_seconds("zs") > 5)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_stolen_lease_stops_the_orphan_runner(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """租约被别的实例抢走：本实例立刻停掉孤儿 runner，且不去释放别人的租约行。

    不这么做的话，本实例会带着旧代次继续推流、继续发出站箱，两条连接互踢到企微的
    踢线熔断（5 分钟 10 次）才收敛。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        async with make_session_factory(db_engine)() as s:
            mine = await s.get(BotLease, bot.id, populate_existing=True)
            assert mine and mine.holder_instance == service.instance_id
            generation = int(mine.generation)
        async with make_session_factory(db_engine)() as s:
            # 心跳做旧与抢占放同一个事务：本实例的租约扫描要么看不到中间态，要么在 acquire
            # 上被行锁挡住、解锁后重算 WHERE（那时持有者已是 gw-other），不会自己抢回来。
            await instances.register(
                s, instance_id="gw-other", service="gateway-wecom", version="dev", capacity=None
            )
            await s.execute(
                update(BotLease)
                .where(BotLease.bot_id == bot.id)
                .values(heartbeat_at=func.now() - text("interval '31 seconds'"))
            )
            stolen = await leases.acquire(
                s, bot_id=bot.id, platform="wecom", instance_id="gw-other"
            )
            assert stolen and stolen.holder_instance == "gw-other"
            await s.commit()
        await _wait(lambda: "bot1" not in fake.connections and not service.runners)
        async with make_session_factory(db_engine)() as s:
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            # 租约仍归 gw-other：孤儿只收自己那一半连接，没有 release 掉别人的行。
            assert lease and lease.holder_instance == "gw-other" and lease.released_at is None
            assert int(lease.generation) == generation + 1
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_bot_drain_releases_the_lease_and_the_bot_is_reacquired(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """按 bot 排空是「换一次连接」，不是永久下线。

    ①②③ 之后企微那边看到旧连接断掉，④ 释放租约时排空标记一并清掉，于是下一轮租约扫描
    立刻重新认领、重新订阅（单实例部署下用户看到的就是一次重连，有备用实例时是交接）。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        old_ws = fake.connections["bot1"]
        subscribes = fake.subscribe_attempts
        async with make_session_factory(db_engine)() as s:
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            assert lease and lease.holder_instance == service.instance_id
            generation = int(lease.generation)
            await leases.request_drain(s, bot.id, by="admin")  # = POST /runtime/drain {"bot_key"}
            await s.commit()
        await _wait(lambda: old_ws.close_code is not None)  # ③ 旧连接真的被关掉了
        await _wait(lambda: fake.subscribe_attempts > subscribes and "bot1" in fake.connections)
        new_ws = fake.connections["bot1"]
        assert new_ws is not old_ws
        async with make_session_factory(db_engine)() as s:
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            assert lease and lease.holder_instance == service.instance_id
            assert int(lease.generation) == generation + 1 and lease.drain_requested_by is None
        # 标记在下一轮扫描之前就清掉了：认领回来的 bot 不会被 drain_bot 再排空一次（不来回踢）
        subscribes = fake.subscribe_attempts
        await asyncio.sleep(1.0)  # 5 个租约间隔
        assert fake.connections.get("bot1") is new_ws and fake.subscribe_attempts == subscribes
        async with make_session_factory(db_engine)() as s:
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            assert lease and lease.holder_instance == service.instance_id
            assert int(lease.generation) == generation + 1
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def _stream_row(db_engine: AsyncEngine, task_id: int):  # type: ignore[no-untyped-def]
    async with make_session_factory(db_engine)() as s:
        return await streams.get(s, task_id)


async def test_dead_stream_hands_the_rest_over_to_the_worker(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """企微判这条流失效（846606）：立刻翻 proactive，offset 只认确认送达的那一段。

    企微的流 10 分钟就死，而 `agent_timeout` 最长能配到 7200 秒。不在这里交接的话，长任务的
    答案会被静默丢掉——流推不动，又没人改走主动推送。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)  # pending_text="正文"
        await _wait(lambda: bool(fake.stream_contents(req)))
        # 时钟往后拨：第一帧就此落在 errcode 的延迟预算之外，算它确认送达；预算之内发出去
        # 的（下面那一帧）一律不算——错误码是异步回的，谁被拒只能按时间推断。
        clock = [time.monotonic() + 100.0]
        service.runners[bot.id].pusher.clock = lambda: clock[0]
        async with make_session_factory(db_engine)() as s:
            await streams.update(s, task_id, pending_text="正文又写了一大段")
            await s.commit()
        await _wait(lambda: len(fake.stream_contents(req)) >= 2, timeout=8)
        # 第二帧被拒：它的内容没到用户那边，能确认送达的只有第一帧的「正文」两个字。
        await fake.respond_errcode("bot1", req, 846606, "stream not exist")

        async def handed_off() -> bool:
            row = await _stream_row(db_engine, task_id)
            return bool(row and row.delivery_mode == "proactive")

        await _wait(handed_off)
        row = await _stream_row(db_engine, task_id)
        assert row and row.background_state["offset"] == len("正文")
        assert row.finish_pushed_at is not None
        # worker 收尾时，complete 的返回值就告诉它「终稿归我推，从 offset 开始」。
        async with make_session_factory(db_engine)() as s:
            done = await streams.complete(s, task_id, final_text="正文又写了一大段，完成")
            await s.commit()
        assert done.delivery_mode == "proactive" and done.offset == len("正文")
        pushed = len(fake.stream_contents(req))
        await asyncio.sleep(0.6)
        assert len(fake.stream_contents(req)) == pushed  # 死掉的流不再被推
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_rejected_finish_frame_sends_the_final_text_via_outbox(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """流已经完成、finish 帧却被拒：终稿改走主动推送，绝不是「标一下 finish_pushed 就算了」。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)
        await _wait(lambda: bool(fake.stream_contents(req)))
        # 时钟往后拨：第一帧就此落在 errcode 的延迟预算之外，算它确认送达；预算之内发出去
        # 的（下面那一帧）一律不算——错误码是异步回的，谁被拒只能按时间推断。
        clock = [time.monotonic() + 100.0]
        service.runners[bot.id].pusher.clock = lambda: clock[0]
        async with make_session_factory(db_engine)() as s:
            await streams.complete(s, task_id, final_text="正文，全部完成")
            await s.commit()
        await _wait(lambda: fake.stream_contents(req)[-1][1])  # finish 帧发出去了
        await fake.respond_errcode("bot1", req, 846608, "stream finished")

        async def queued() -> bool:
            async with make_session_factory(db_engine)() as s:
                return (await s.execute(select(OutboxItem))).scalar_one_or_none() is not None

        await _wait(queued)
        async with make_session_factory(db_engine)() as s:
            item = (await s.execute(select(OutboxItem))).scalar_one()
            assert item.dedupe_key == f"{task_id}:send:final" and item.kind == "send"
            # 会话 id：这条流的 reply_context 没带，回表用任务的会话键兜底
            assert item.target == {"chat_id": "zs"}
            assert item.payload["markdown"] == "正文，全部完成"[len("正文") :]
        row = await _stream_row(db_engine, task_id)
        assert row and row.finish_pushed_at is not None
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_drain_records_the_offset_it_delivered_and_takeover_records_zero(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """排空的 finish 帧带走了当时的正文 → offset 记成那个长度；接管什么都没送 → offset 记 0。

    offset 记错方向就会丢字：worker 的后台推送只发 `[offset:]`。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)

        async def committed_initial_frame():
            row = await _stream_row(db_engine, task_id)
            return row is not None and row.pushed_version >= row.version

        # 对端收到帧时网关事务可能还没提交；等到推送进度落库再测排空。
        await _wait(committed_initial_frame)
        bot_runner = service.runners[bot.id]
        await bot_runner.drain()
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "proactive" and row.finish_pushed_at is not None
        assert row.background_state["offset"] == len("正文")
        assert row.background_state["finish_suffix"] == msg("drain_suffix")
        assert fake.stream_contents(req)[-1][0].endswith(msg("drain_suffix"))
        # 接管：行是上一任代次建的，它的连接早断了，一个字都没送到
        async with make_session_factory(db_engine)() as s:
            task = await tasks_module.enqueue(
                s,
                tasks_module.NewTask(
                    bot_id=bot.id, kind="chat", payload={}, session_key="ls", dedupe_key="t2"
                ),
            )
            assert task
            await streams.create(
                s,
                task_id=task.id,
                bot_id=bot.id,
                platform="wecom",
                stream_id="s-old",
                reply_context={"req_id": "old-req", "chat_id": "ls"},
                lease_generation=bot_runner.generation - 1,
                running_since=datetime.now(UTC),
            )
            await streams.update(s, task.id, pending_text="上一任写的正文")
            await s.commit()
        await bot_runner.pusher.push_pending()

        async def takeover_committed():
            row = await _stream_row(db_engine, task.id)
            return (
                row is not None
                and row.delivery_mode == "proactive"
                and row.finish_pushed_at is not None
            )

        # 后台 pusher 也会同时扫描，手动调用可能因忙碌立即返回。
        await _wait(takeover_committed)
        taken = await _stream_row(db_engine, task.id)
        assert (
            taken and taken.delivery_mode == "proactive" and taken.background_state["offset"] == 0
        )
        assert taken.background_state["takeover"] is True and taken.finish_pushed_at is not None
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_drain_loses_the_race_to_the_worker_and_falls_back_to_outbox(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """worker 的 complete 抢在排空的 UPDATE 之前落地：已完成的流不许被改成 proactive。

    改了的话，worker 那边看到的还是 stream（它先提交的），两边都以为对方会送终稿，用户只
    收到排空那一帧的半截正文。这里让 worker 在「排空读行」与「排空翻行」之间完成，复现的
    正是那个交错。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)
        await _wait(lambda: bool(fake.stream_contents(req)))
        bot_runner = service.runners[bot.id]
        async with make_session_factory(db_engine)() as s:
            stale = await streams.get(s, task_id)  # 排空读到的那一版（还没完成）
        assert stale and not stale.is_complete
        async with make_session_factory(db_engine)() as s:
            done = await streams.complete(s, task_id, final_text="正文，以及余下的答案")
            await s.commit()
        assert done.delivery_mode == "stream"  # worker：这条流还归网关推，我不用管终稿
        async with make_session_factory(db_engine)() as s:
            await bot_runner._drain_stream(s, stale, datetime.now(UTC))
            await s.commit()
        async with make_session_factory(db_engine)() as s:
            item = (await s.execute(select(OutboxItem))).scalar_one()
            assert item.dedupe_key == f"{task_id}:send:final"
            assert item.payload["markdown"] == "正文，以及余下的答案"[len("正文") :]
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "stream" and row.finish_pushed_at is not None
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_disabled_bot_is_dropped_by_the_lease_poll_without_notify(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """停用通知丢了（LISTEN 重连窗口）：租约扫描每轮复核 enabled，几秒内照样断连交租约。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        async with make_session_factory(db_engine)() as s:
            await s.execute(update(Bot).where(Bot.id == bot.id).values(enabled=False))
            await s.commit()  # 故意不发 config_changed

        async def released() -> bool:
            async with make_session_factory(db_engine)() as s:
                lease = await s.get(BotLease, bot.id, populate_existing=True)
                return bool(lease and lease.holder_instance is None)

        await _wait(released, timeout=10)
        assert not service.runners and "bot1" not in fake.connections
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_lease_round_does_not_release_a_bot_that_is_still_draining(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """排空进行中（runner 已摘、连接还在关）：并发的租约扫描不许把租约提前交出去。

    交早了就是「释放租约」跑到「关连接」前面：新实例（或本实例）立刻认领出 gen+1，而排空尾声那次
    release 又把新租约放掉，连接来回断三次。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        bot_runner = service.runners[bot.id]
        order: list[str] = []
        real_stop = bot_runner.stop

        async def slow_stop() -> None:
            order.append("stop_begin")
            await asyncio.sleep(0.8)  # ③ 关 WS 很慢
            await real_stop()
            order.append("stop_end")

        bot_runner.stop = slow_stop  # type: ignore[method-assign]
        async with make_session_factory(db_engine)() as s:
            before = await s.get(BotLease, bot.id, populate_existing=True)
            assert before
            generation = int(before.generation)
        draining = asyncio.create_task(service.drain_bot(bot.id))
        await _wait(lambda: "stop_begin" in order)
        await service._lease_round()  # 排空进行中撞上一轮扫描
        async with make_session_factory(db_engine)() as s:
            lease = await s.get(BotLease, bot.id, populate_existing=True)
            assert lease and lease.holder_instance == service.instance_id
            assert int(lease.generation) == generation and lease.released_at is None
        await asyncio.wait_for(draining, 10)
        assert order == ["stop_begin", "stop_end"]  # ④ 在 ③ 之后
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_rejected_send_goes_back_to_the_queue_and_is_retried(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """846607 是异步回的：`ws.send` 成功不等于送到，标了 sent 就不管会静默丢主动推送。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        service.runners[bot.id].outbox.rate_limit_backoff = 0.2  # 会话退避缩短，别拖长用例
        fake.send_rejections.append((846607, "rate limited"))
        async with make_session_factory(db_engine)() as s:
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="send",
                dedupe_key="z:send:1",
                target={"chat_id": "zs"},
                payload={"markdown": "后台推送"},
            )
            await s.commit()
        await fake.wait_frame(lambda f: f["cmd"] == "aibot_send_msg")

        # NACK arrives while send_confirmed is waiting, before any sent commit.
        async def requeued() -> bool:
            async with make_session_factory(db_engine)() as s:
                row = (await s.execute(select(OutboxItem))).scalar_one()
                return bool(
                    row.status == "pending"
                    and row.attempts == 1
                    and row.not_before > datetime.now(UTC)
                    and row.sent_at is None
                )

        await _wait(requeued)
        async with make_session_factory(db_engine)() as s:
            # 退避 10 秒，用例不等：把 not_before 拨到过去，模拟「不再被拒」的那一次重试
            await s.execute(
                update(OutboxItem).values(not_before=func.now() - text("interval '1 second'"))
            )
            await s.commit()
        await _wait(lambda: len(fake.sent_messages()) == 2, timeout=8)
        assert [b["markdown"]["content"] for b in fake.sent_messages()] == ["后台推送"] * 2

        async def resent() -> bool:
            async with make_session_factory(db_engine)() as s:
                row = (await s.execute(select(OutboxItem))).scalar_one()
                return bool(row.status == "sent" and row.attempts == 1 and row.sent_at is not None)

        # 同上：重试那一帧到达假企微时，`mark_sent` 还没 commit。
        await _wait(resent)
        async with make_session_factory(db_engine)() as s:
            row = (await s.execute(select(OutboxItem))).scalar_one()
            assert row.status == "sent" and row.attempts == 1 and row.sent_at is not None
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_rejected_proactive_finish_frame_queues_the_missing_gap(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """主动模式那一帧 finish 被企微拒了：`confirmed..offset` 这一段谁都不会送，网关自己补。

    默认 `agent_timeout` 恰好等于企微流的 10 分钟上限，这条路径是常态不是边角：worker 先翻
    proactive 并把 offset 记成当前正文长度，网关渲染 finish 推出去（`ws.send` 成功就标
    `finish_pushed_at`），企微几秒后才回 846606——行既不是「未完成待交接」也不是「已完成待
    补终稿」，两个老分支都不命中；而 worker 此后只推 `[offset:]`。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)  # pending_text="正文"
        await _wait(lambda: bool(fake.stream_contents(req)))
        pusher = service.runners[bot.id].pusher
        # 时钟往后拨 100 秒：第一帧就此落在「errcode 延迟预算」之外，算它确认送达。
        clock = [time.monotonic() + 100.0]
        pusher.clock = lambda: clock[0]
        full = "正文又写了一大段"
        async with make_session_factory(db_engine)() as s:
            await streams.update(
                s,
                task_id,
                pending_text=full,
                delivery_mode="proactive",
                background_state={
                    "mode": "incremental",
                    "offset": len(full),
                    "finish_suffix": msg("timeout_background_low"),
                    "switched_at": datetime.now(UTC).isoformat(),
                },
            )
            await s.commit()

        async def finished() -> bool:
            row = await _stream_row(db_engine, task_id)
            return bool(row and row.finish_pushed_at is not None)

        await _wait(lambda: bool(fake.stream_contents(req)) and fake.stream_contents(req)[-1][1])
        await _wait(finished)
        clock[0] += 1.0  # 错误码在预算之内回来：被拒的那一帧不算送达
        await fake.respond_errcode("bot1", req, 846606, "stream not exist")

        async def queued() -> bool:
            async with make_session_factory(db_engine)() as s:
                return (await s.execute(select(OutboxItem))).scalar_one_or_none() is not None

        await _wait(queued)
        async with make_session_factory(db_engine)() as s:
            item = (await s.execute(select(OutboxItem))).scalar_one()
            assert item.kind == "send" and item.dedupe_key == f"{task_id}:send:finish_gap"
            # 会话 id 照旧回表兜底；内容恰是「已确认送达的长度」到 offset 之间那一段
            assert item.target == {"chat_id": "zs"}
            assert item.payload["markdown"] == full[len("正文") :]
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_confirmed_length_only_counts_frames_older_than_the_errcode_budget(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """进行中的推送每秒一帧，而错误码可能几秒后才回：预算之内的帧一律不算确认送达。

    只记「最近两帧」的话，被拒的那一帧前面还压着好几帧，offset 会被估高——高出来的那一段
    worker 不推（它只发 `[offset:]`）、网关也不推（流已经死了）。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)
        await _wait(lambda: bool(fake.stream_contents(req)))
        pusher = service.runners[bot.id].pusher
        base = time.monotonic() + 100.0
        clock = [base]
        pusher.clock = lambda: clock[0]
        pusher.errcode_budget = 3.0
        probe = TaskStream(
            task_id=task_id, stream_id="s1", reply_context={"req_id": req}, pending_text=""
        )
        for tick, size in ((0.0, 3), (2.0, 5), (2.5, 7)):
            clock[0] = base + tick
            probe.pending_text = "甲" * size
            assert await pusher.send_stream(probe, probe.pending_text, finish=False)
        clock[0] = base + 3.0  # 错误码到达：预算之内的后两帧不算数，只认 t=base 那一帧
        await pusher.note_stream_dead("s1", task_id)
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "proactive"
        assert row.background_state["offset"] == 3
        # 另一条流：所有帧都在预算之内，一个字都不敢算送达
        other = TaskStream(
            task_id=task_id, stream_id="s2", reply_context={"req_id": req}, pending_text="乙乙乙乙"
        )
        assert await pusher.send_stream(other, other.pending_text, finish=False)
        assert pusher.delivered_len("s2") == 0
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_drain_waits_for_a_late_rejection_of_its_finish_frame(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """排空那帧 finish 被企微异步拒掉：③ 关连接之前留一个有界宽限，缺口才有人补。

    `ws.send` 成功只说明写进了连接。不等的话连接先断，846606/846608 永远回不来，那一帧本
    该带走的正文（`offset` 之前的部分）就再也没人送——worker 从 offset 之后才开始推。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)  # pending_text="正文"
        await _wait(lambda: bool(fake.stream_contents(req)))
        service.runners[bot.id].errcode_grace = 1.5
        fake.reject_finish_frames(errcode=846608, errmsg="stream finished", delay=0.2)
        await service.drain_bot(bot.id)

        # NACK is observed before the handoff offset is committed; the worker
        # now owns the entire undelivered prefix, so no finish_gap is needed.
        async with make_session_factory(db_engine)() as s:
            assert (await s.execute(select(OutboxItem))).scalar_one_or_none() is None
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "proactive"
        assert row.background_state["offset"] == 0
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_drain_without_a_finish_frame_does_not_wait_for_errcodes(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """没发过 finish 帧就没有待观测的拒绝：宽限不许平白加在每一次排空上。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        service.runners[bot.id].errcode_grace = 30.0  # 真等下去就会把用例拖爆
        started = time.monotonic()
        await service.drain_bot(bot.id)  # 这个 bot 一条活跃的流都没有
        assert time.monotonic() - started < 5.0
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_concurrent_drain_of_the_same_bot_only_drains_once(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """同一个 bot 并发两次排空：第二次立刻返回，且不许抹掉第一次的排空标记。

    抹掉了的话，并发的租约扫描会把「持有租约却没有连接」的这一行提前 release——排空的「释放租约」
    跑到「关连接」前面，连接来回断。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        bot_runner = service.runners[bot.id]
        stops: list[str] = []
        real_stop = bot_runner.stop

        async def slow_stop() -> None:
            stops.append("begin")
            await asyncio.sleep(0.5)  # ③ 关 WS 很慢，第二次调用正好撞进来
            await real_stop()

        bot_runner.stop = slow_stop  # type: ignore[method-assign]
        first = asyncio.create_task(service.drain_bot(bot.id))
        await _wait(lambda: bool(stops))
        await service.drain_bot(bot.id)  # 第二次：立刻返回
        assert bot.id in service._draining  # 标记仍在（抹掉了就轮到租约扫描去还租约）
        await asyncio.wait_for(first, 10)
        assert stops == ["begin"]
        assert bot.id not in service._draining
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()
