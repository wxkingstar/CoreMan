import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import streams, tasks
from coreman.core.db.models import ChatLog, OutboxItem, TaskStream
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.background import TimeoutSupervisor, Timing
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.replies import open_stream
from coreman.runtime.worker.stream_writer import StreamWriter
from tests.fakes.fake_relay import DONE, FINISH, SCENARIOS, FakeRelay, _chunk, _text, _tool
from tests.integration.test_chat_handler import chat_task, stream_of
from tests.integration.worker_helpers import build_ctx, seed_bot

FAST = Timing(
    wecom_background_after=1,
    pre_warning=0.4,
    bg_min_interval=0.2,
    bg_max_wait=1.0,
    bg_degrade_after=24,
    bg_degraded_interval=90,
    hard_ttl=7200,
)


def _long_scenario() -> list[str]:
    frames = [": ping\n\n"]
    for i in range(8):
        frames += [_text(f"段落{i}。"), _tool("Bash", f"t{i}")]
    return frames + [FINISH, DONE]


RELAY_ERROR_LINE = "⚠️ claude exited without producing any output"


def _long_error_scenario() -> list[str]:
    """先吐够内容让它切到后台，再以 x_relay_error 收尾。"""
    frames = [": ping\n\n"]
    for i in range(6):
        frames += [_text(f"段落{i}。"), _tool("Bash", f"t{i}")]
    return frames + [
        _chunk({"role": "assistant", "content": RELAY_ERROR_LINE}, error=True),
        _chunk({"role": "", "content": ""}, finish="stop", error=True),
        DONE,
    ]


SWITCH_WAIT_SECONDS = 8.0


async def _wait_proactive(
    session: AsyncSession, task_id: int, runner: asyncio.Task[None]
) -> TaskStream:
    """等 worker 把流切成 proactive——tick 粒度 1 秒，固定 sleep 太脆。

    只刷新 task_streams 这一行：`expire_all()` 会顺手把 handler 正拿着的那个 Task 实例也置
    失效，它下一次读属性就会在同一个 session 上并发发查询（InvalidRequestError）。
    """
    stmt = (
        select(TaskStream)
        .where(TaskStream.task_id == task_id)
        .execution_options(populate_existing=True)
    )
    for _ in range(int(SWITCH_WAIT_SECONDS / 0.05)):
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None and row.delivery_mode == "proactive":
            return row
        if runner.done():
            runner.result()  # 处理器先跑完了：有异常就把真正的原因抛出来
            raise AssertionError("处理器已结束但流没切到后台")
        await asyncio.sleep(0.05)
    raise AssertionError("流没有在期限内切到后台")


async def test_switches_to_background_and_pushes_increments(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    SCENARIOS["long"] = _long_scenario
    bot, _, _ = await seed_bot(db_session, verbosity=1)
    await db_session.commit()
    t = await chat_task(db_session, bot, "慢任务")
    fake = FakeRelay("long", chunk_delay=0.15)
    ctx = build_ctx(db_engine, t, relay_client_factory=lambda _r: fake.client())
    handler = ChatTaskHandler(timing=FAST)
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)
    s = await stream_of(db_session, t.id)
    assert s.delivery_mode == "proactive" and s.is_complete
    assert s.background_state["mode"] == "incremental"
    assert (
        msg("timeout_background_low") in s.background_state["finish_suffix"]
        and s.session_url in s.background_state["finish_suffix"]
    )
    assert msg("timeout_pre_warning") in s.thinking_md
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert items and all(i.kind == "send" and i.target == {"chat_id": "zs"} for i in items)
    assert (
        items[0].payload["markdown"].startswith(msg("bg_progress_prefix"))
        and items[0].dedupe_key == f"{t.id}:send:1"
    )
    assert items[-1].payload["markdown"].startswith(msg("bg_done_prefix")) or items[-1].payload[
        "markdown"
    ] == msg("bg_done_plain")
    assert "耗时" not in " ".join(i.payload["markdown"] for i in items)  # 后台模式不再单发完成提醒
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert log.status == "success"
    assert (await tasks.get(db_session, t.id)).status == "succeeded"  # type: ignore[union-attr]


async def test_switch_keeps_the_offset_the_gateway_already_delivered(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """网关先排空（已投递前 N 个字）再轮到两阶段切换：offset 不许被「当前长度」覆盖。

    覆盖了的话，排空的 finish 与第一条后台推送之间的那一大段正文谁都不会发。
    """
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "先被排空再切后台")
    tid = t.id
    ctx = build_ctx(db_engine, t)
    writer = StreamWriter(ctx)
    async with make_session_factory(db_engine)() as s:
        await open_stream(s, ctx, reply_context={"req_id": "r", "chat_id": "zs"})
        await s.commit()
    writer.add_text("排空时已投递")
    await writer.flush(force=True)
    delivered = len(writer.pending_text)
    async with make_session_factory(db_engine)() as s:
        # 网关 drain：翻 proactive 并记下「finish 帧带走了多少」
        await streams.update(
            s,
            tid,
            delivery_mode="proactive",
            background_state={
                "mode": "proactive",
                "offset": delivered,
                "finish_suffix": msg("drain_suffix"),
                "switched_at": "",
            },
        )
        await s.commit()
    writer.add_text("排空之后又写了很长一段")
    supervisor = TimeoutSupervisor(
        ctx, writer, agent_timeout=1, timing=FAST, session_url="u", chat_id="zs"
    )
    await supervisor._switch(100.0)
    db_session.expire_all()
    row = await stream_of(db_session, tid)
    assert row.background_state["offset"] == delivered
    assert supervisor.pusher.offset == delivered
    # 超时提示与切换时刻照常补上，模式也按 verbosity 落定
    assert msg("timeout_background_low") in row.background_state["finish_suffix"]
    assert row.background_state["switched_at"] and row.background_state["mode"] == "incremental"


async def test_gateway_proactive_is_adopted_before_agent_timeout(db_engine, db_session):
    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "接管后继续")
    ctx = build_ctx(db_engine, task)
    writer = StreamWriter(ctx)
    async with make_session_factory(db_engine)() as session:
        await open_stream(session, ctx, reply_context={"req_id": "r", "chat_id": "zs"})
        await streams.switch_to_proactive(
            session, task.id, state={"offset": 3, "finish_suffix": "drain"}
        )
        await session.commit()
    writer.add_text("已送达尚未展示")
    await writer.flush(force=True)
    supervisor = TimeoutSupervisor(
        ctx, writer, agent_timeout=600, timing=FAST, session_url="u", chat_id="zs", started_at=100
    )
    assert await supervisor.tick(101) == "switched"
    assert supervisor.pusher.offset == 3
    await supervisor.on_progress(200, writer.pending_text, [len(writer.pending_text)])
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert "尚未展示" in item.payload["markdown"] and "已送达" not in item.payload["markdown"]
    row = await stream_of(db_session, task.id)
    assert row.background_state["finish_suffix"] == "drain"


async def test_high_verbosity_pushes_only_final(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    SCENARIOS["long"] = _long_scenario
    bot, _, _ = await seed_bot(db_session, verbosity=3)
    await db_session.commit()
    t = await chat_task(db_session, bot, "慢任务")
    ctx = build_ctx(
        db_engine, t, relay_client_factory=lambda _r: FakeRelay("long", chunk_delay=0.15).client()
    )
    await ChatTaskHandler(timing=FAST).run(ctx)
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert len(items) == 1 and "段落7。" in items[0].payload["markdown"]
    s = await stream_of(db_session, t.id)
    assert (
        msg("timeout_background_high") in s.background_state["finish_suffix"]
        and s.background_state["mode"] == "final_only"
    )


async def test_hard_ttl_cancels_and_notifies(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    await db_session.commit()
    t = await chat_task(db_session, bot, "永远不结束")
    fake = FakeRelay("slow", chunk_delay=5.0)
    ctx = build_ctx(db_engine, t, relay_client_factory=lambda _r: fake.client())
    await ChatTaskHandler(
        timing=Timing(wecom_background_after=1, pre_warning=0.3, hard_ttl=2.0)
    ).run(ctx)
    await ctx.chat_logs.drain(5)
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "timed_out"
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert any(msg("bg_ttl_expired", link="").split("{")[0] in i.payload["markdown"] for i in items)
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert log.status == "timeout" and fake.aborted == 1
    assert uuid.UUID(str(bot.id))


async def test_user_stop_after_switch_is_not_done(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """切后台之后被用户停掉：最后一条推送得是「已停止」，不能冠 ✅ 任务已完成。"""
    bot, _, _ = await seed_bot(db_session)
    await db_session.commit()
    t = await chat_task(db_session, bot, "慢任务")
    tid = t.id  # 下面要 expire_all()，之后再读 t 的属性会同步触发 IO
    fake = FakeRelay("slow", chunk_delay=0.4)
    ctx = build_ctx(db_engine, t, relay_client_factory=lambda _r: fake.client())
    runner = asyncio.create_task(ChatTaskHandler(timing=FAST).run(ctx))
    try:
        await _wait_proactive(db_session, tid, runner)
        assert await tasks.request_cancel(db_session, tid, "user_stop") is True
        await db_session.commit()
        await ctx.heartbeat()
        await asyncio.wait_for(runner, 10)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    db_session.expire_all()
    s = await stream_of(db_session, tid)
    assert s.delivery_mode == "proactive" and s.is_complete
    assert (await tasks.get(db_session, tid)).status == "cancelled"  # type: ignore[union-attr]
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == tid))).scalar_one()
    assert log.status == "stopped"
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    last = items[-1].payload["markdown"]
    assert msg("task_stopped_suffix").strip() in last
    assert not last.startswith(msg("bg_done_prefix")) and last != msg("bg_done_plain")


async def test_relay_error_after_switch_is_not_done(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """切后台之后 relay 回错：最后一条推送是错误正文，同样不冠 ✅。"""
    SCENARIOS["long_error"] = _long_error_scenario
    bot, _, _ = await seed_bot(db_session)
    await db_session.commit()
    t = await chat_task(db_session, bot, "会出错的慢任务")
    ctx = build_ctx(
        db_engine,
        t,
        relay_client_factory=lambda _r: FakeRelay("long_error", chunk_delay=0.15).client(),
    )
    await ChatTaskHandler(timing=FAST).run(ctx)
    await ctx.chat_logs.drain(5)
    s = await stream_of(db_session, t.id)
    assert s.delivery_mode == "proactive"
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    last = items[-1].payload["markdown"]
    assert RELAY_ERROR_LINE in last
    assert not last.startswith(msg("bg_done_prefix")) and last != msg("bg_done_plain")
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert log.status == "error" and log.error_code == "x_relay_error"
    assert (await tasks.get(db_session, t.id)).status == "failed"  # type: ignore[union-attr]


async def _take_over(db_engine: AsyncEngine, task_id: int, runner: asyncio.Task[None]) -> None:
    """模拟网关接管：流一建好就被别人置成 proactive 且已经推过 finish。

    supervisor 用默认 `Timing`（预警 20 秒、agent_timeout 600 秒）所以本任务自己不会切后台，
    收尾只能走 `_push_if_proactive` 那一路——被接管的流没人再跟，终稿必须改走 outbox。
    """
    stmt = (
        select(TaskStream)
        .where(TaskStream.task_id == task_id)
        .execution_options(populate_existing=True)
    )
    for _ in range(int(SWITCH_WAIT_SECONDS / 0.05)):
        async with make_session_factory(db_engine)() as s:
            if (await s.execute(stmt)).scalar_one_or_none() is not None:
                await streams.update(s, task_id, delivery_mode="proactive")
                await streams.mark_finish_pushed(s, task_id)
                await s.commit()
                return
        if runner.done():
            runner.result()  # 处理器先跑完了：有异常就把真正的原因抛出来
            raise AssertionError("处理器已经结束，没来得及接管")
        await asyncio.sleep(0.05)
    raise AssertionError("流没有在期限内建好")


async def test_taken_over_success_pushes_done_prefix(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """被接管 + 成功收尾：终稿进 outbox，冠 ✅，幂等键是 `{task_id}:send:final`。"""
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "被接管的任务")
    tid = t.id  # 下面要 expire_all()，之后再读 t 的属性会同步触发 IO
    ctx = build_ctx(
        db_engine, t, relay_client_factory=lambda _r: FakeRelay("slow", chunk_delay=0.2).client()
    )
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        await _take_over(db_engine, tid, runner)
        await asyncio.wait_for(runner, 20)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    db_session.expire_all()
    s = await stream_of(db_session, tid)
    assert s.delivery_mode == "proactive" and s.is_complete
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert len(items) == 1 and items[0].kind == "send" and items[0].target == {"chat_id": "zs"}
    assert items[0].dedupe_key == f"{tid}:send:final"
    markdown = items[0].payload["markdown"]
    assert markdown == msg("bg_done_prefix") + s.final_text.removesuffix(msg("done_suffix"))
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == tid))).scalar_one()
    assert log.status == "success"
    assert (await tasks.get(db_session, tid)).status == "succeeded"  # type: ignore[union-attr]


async def test_taken_over_relay_error_is_not_done(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """被接管 + relay 回错：同一条 `:send:final`，推的是错误正文，绝不冠 ✅。"""
    SCENARIOS["long_error"] = _long_error_scenario  # 这里只借它的「慢帧 + x_relay_error 收尾」
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "被接管又出错的任务")
    tid = t.id
    ctx = build_ctx(
        db_engine,
        t,
        relay_client_factory=lambda _r: FakeRelay("long_error", chunk_delay=0.15).client(),
    )
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        await _take_over(db_engine, tid, runner)
        await asyncio.wait_for(runner, 20)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    db_session.expire_all()
    s = await stream_of(db_session, tid)
    assert s.delivery_mode == "proactive" and s.is_complete
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert len(items) == 1 and items[0].dedupe_key == f"{tid}:send:final"
    markdown = items[0].payload["markdown"]
    assert RELAY_ERROR_LINE in markdown
    assert not markdown.startswith(msg("bg_done_prefix")) and markdown != msg("bg_done_plain")
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == tid))).scalar_one()
    assert log.status == "error" and log.error_code == "x_relay_error"
    assert (await tasks.get(db_session, tid)).status == "failed"  # type: ignore[union-attr]


async def test_switch_does_not_overwrite_a_gateway_switch_committed_while_it_reads(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """网关的切换恰好落在「读行」与「写行」之间：offset 照样不许被当前长度覆盖。

    读行不上锁的话，worker 读到的还是 stream（网关那笔尚未提交），等它的 UPDATE 拿到行锁
    时网关已经落地，offset 被改写成当前正文长度——网关那帧 finish 之后的一大段正文两头都
    不发。这里让网关先拿住行锁、`_switch` 的读撞上来，复现的就是这个交错。
    """
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "读与写之间被网关抢先")
    tid = t.id
    ctx = build_ctx(db_engine, t)
    writer = StreamWriter(ctx)
    async with make_session_factory(db_engine)() as s:
        await open_stream(s, ctx, reply_context={"req_id": "r", "chat_id": "zs"})
        await s.commit()
    writer.add_text("排空时已投递")
    await writer.flush(force=True)
    delivered = len(writer.pending_text)
    writer.add_text("排空之后又写了很长一段")
    supervisor = TimeoutSupervisor(
        ctx, writer, agent_timeout=1, timing=FAST, session_url="u", chat_id="zs"
    )
    locked = asyncio.Event()
    racing: list[asyncio.Task[None]] = []

    async def gateway_switch() -> None:
        async with make_session_factory(db_engine)() as s:
            await streams.switch_to_proactive(
                s,
                tid,
                state={
                    "mode": "proactive",
                    "offset": delivered,
                    "finish_suffix": msg("drain_suffix"),
                    "switched_at": "",
                },
            )
            locked.set()  # 行锁到手，但先不提交
            await asyncio.sleep(0.3)  # 让 _switch 的读撞上来
            await s.commit()

    real_flush = writer.flush

    async def flush_then_let_the_gateway_in(force: bool = False) -> bool:
        # `_switch` 的第一件事是强制 flush（它自己也要写这一行），放它先走完再开抢，
        # 不然两笔写互等，复现不出「读到旧值、写在人家后面」的那个交错。
        ok = await real_flush(force)
        racing.append(asyncio.create_task(gateway_switch()))
        await asyncio.wait_for(locked.wait(), 5)
        return ok

    writer.flush = flush_then_let_the_gateway_in  # type: ignore[method-assign]
    await supervisor._switch(100.0)
    await asyncio.gather(*racing)
    db_session.expire_all()
    row = await stream_of(db_session, tid)
    assert row.background_state["offset"] == delivered
    assert supervisor.pusher.offset == delivered
    assert msg("timeout_background_low") in row.background_state["finish_suffix"]


@pytest.mark.parametrize("platform", ["wecom", "feishu"])
@pytest.mark.parametrize("legacy_timeout", [30, 7200])
async def test_platform_policy_ignores_legacy_settings_after_ten_minutes(
    db_engine, db_session, platform, legacy_timeout
):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = platform
    bot.agent_timeout_seconds = legacy_timeout
    await db_session.commit()
    task = await chat_task(db_session, bot, "long task")
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: fake.client())
    await ctx.settings_store.set("agent_timeout_seconds", legacy_timeout)

    class ElapsedHandler(ChatTaskHandler):
        def _after_supervisor(self, pre):
            assert pre.supervisor.agent_timeout == (580 if platform == "wecom" else None)
            # 模拟已经运行 700 秒，仍未达到独立的硬 TTL。
            pre.supervisor.started_at -= 700

    await ElapsedHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    row = await stream_of(db_session, task.id)
    assert row.is_complete
    if platform == "feishu":
        assert row.delivery_mode == "stream"
        assert msg("timeout_pre_warning") not in row.thinking_md
    else:
        assert row.delivery_mode == "proactive"
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == task.id))).scalar_one()
    assert log.status == "success"
