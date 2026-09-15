import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    ChatLog,
    ChatSession,
    InboundEvent,
    OutboxItem,
    TaskStream,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg
from coreman.core.prompting.defaults import SPEAKER_CHANGED_LINE
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.worker_helpers import build_ctx, seed_bot


def message(
    text: str,
    *,
    platform: str = "wecom",
    sender: str = "zs",
    chat_type: str = "single",
    chat_id: str | None = None,
    parts=None,
    kind="message",
):  # type: ignore[no-untyped-def]
    return {
        "platform": platform,
        "kind": kind,
        "chat_type": chat_type,
        "chat_id": chat_id or sender,
        "sender": {"platform_user_id": sender, "open_id": None},
        "message_id": f"m-{uuid.uuid4()}",
        "mentions_bot": False,
        "parts": parts if parts is not None else [{"type": "text", "text": text}],
        "card_action": None,
        "raw": {},
    }


async def chat_task(session: AsyncSession, bot: Bot, text: str, **kw):  # type: ignore[no-untyped-def]
    m = message(text, platform=bot.platform, **kw)
    ev = InboundEvent(
        bot_id=bot.id,
        platform=m["platform"],
        platform_msg_id=m["message_id"],
        kind=m["kind"],
        chat_type=m["chat_type"],
        chat_id=m["chat_id"],
        sender_platform_user_id=m["sender"]["platform_user_id"],
        payload=m,
        reply_context={"gateway_instance": "gw", "req_id": f"r-{m['message_id']}"},
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={
                "message": m,
                "bot_key": bot.bot_key,
                "platform_user_id": m["sender"]["platform_user_id"],
            },
            session_key=m["chat_id"],
            inbound_event_id=ev.id,
        ),
    )
    await session.commit()
    assert t
    # tasks.claimed_by 有外键，认领之前实例必须先登记（与 test_command_handler.py 同）。
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == t.id
    return claimed


async def run(engine: AsyncEngine, task, relay: FakeRelay):  # type: ignore[no-untyped-def]
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    return ctx


async def stream_of(session: AsyncSession, task_id: int) -> TaskStream:
    return (
        await session.execute(select(TaskStream).where(TaskStream.task_id == task_id))
    ).scalar_one()


async def test_normal_round_trip_new_session_then_resume(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row, _ = await seed_bot(db_session, env={"DB_PASSWORD": "pw"})
    fake = FakeRelay("normal")
    t1 = await chat_task(db_session, bot, "你好")
    await run(db_engine, t1, fake)
    s = await stream_of(db_session, t1.id)
    assert s.is_complete and s.final_text and s.final_text.endswith(msg("done_suffix"))
    assert (
        s.final_text.startswith("📎 查看实时聊天记录：[链接>>](http://relay.test:80/session/")
        and "你好，世界。" in s.final_text
    )
    assert (
        "🔧 **Bash**" in s.thinking_md
        and s.thinking_md.startswith(msg("thinking_start"))
        and s.session_url
    )
    assert s.stream_id.startswith("bot:sales_bot|user:zs|ts:")
    row = await tasks.get(db_session, t1.id)
    assert row and row.status == "succeeded"
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert (
        log.status == "success"
        and log.input_tokens == 100
        and log.output_tokens == 20
        and log.cache_read_tokens == 80
    )
    assert (
        log.tools_used == ["Bash"]
        and log.message_content == "你好"
        and log.chat_type == "single"
        and log.relay_server_id == relay_row.id
    )
    body = fake.requests[0]
    assert body["session_id"] == str(
        (await db_session.execute(select(ChatSession))).scalar_one().relay_session_id
    )
    assert (
        body["env_vars"]["DB_PASSWORD"] == "pw"
        and body["env_vars"]["BOT_KEY"] == "sales_bot"
        and body["env_vars"]["AGENT_CHAT_TYPE"] == "single"
    )
    assert (
        "BOT_USER_LOGIN" not in body["env_vars"]
        and "identity_unknown" in body["messages"][0]["content"]
    )
    assert "你是销售" in body["messages"][0]["content"] and body["messages"][1]["content"] == "你好"
    t2 = await chat_task(db_session, bot, "继续")
    await run(db_engine, t2, fake)
    s2 = await stream_of(db_session, t2.id)
    assert s2.final_text and not s2.final_text.startswith("📎")
    assert fake.requests[1]["session_id"] == body["session_id"]


async def test_known_identity_and_group_speaker_change(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    u = User(login_name="zhangsan", display_name="张三")
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zs"))
    await db_session.commit()
    fake = FakeRelay("normal")
    await run(
        db_engine,
        await chat_task(db_session, bot, "hi", chat_type="group", chat_id="g1", sender="zs"),
        fake,
    )
    sp = fake.requests[0]["messages"][0]["content"]
    assert (
        "[SYS_USER] user_id=zs, login=zhangsan, name=张三" in sp and SPEAKER_CHANGED_LINE not in sp
    )
    assert (
        fake.requests[0]["env_vars"]["BOT_USER_LOGIN"] == "zhangsan"
        and fake.requests[0]["env_vars"]["AGENT_CHAT_ID"] == "g1"
    )
    u2 = User(login_name="lisi", display_name="李四")
    db_session.add(u2)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u2.id, platform="wecom", platform_user_id="ls"))
    await db_session.commit()
    await run(
        db_engine,
        await chat_task(db_session, bot, "me too", chat_type="group", chat_id="g1", sender="ls"),
        fake,
    )
    assert SPEAKER_CHANGED_LINE in fake.requests[1]["messages"][0]["content"]
    logs = (await db_session.execute(select(ChatLog).order_by(ChatLog.id))).scalars().all()
    assert [(x.user_login, x.chat_type, x.chat_id) for x in logs] == [
        ("zhangsan", "group", "g1"),
        ("lisi", "group", "g1"),
    ]


async def test_relay_error_empty_and_tools_only(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "a")
    await run(db_engine, t, FakeRelay("relay_error"))
    s = await stream_of(db_session, t.id)
    assert s.is_complete and "⚠️ claude exited" in (s.final_text or "")
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert log.status == "error" and log.error_code == "x_relay_error"
    t2 = await chat_task(db_session, bot, "b")
    await run(db_engine, t2, FakeRelay("empty"))
    log2 = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t2.id))).scalar_one()
    assert (
        log2.status == "error" and log2.error_code == "empty_stream" and log2.input_tokens is None
    )
    assert (await stream_of(db_session, t2.id)).final_text.startswith("⚠️ AI 服务返回了空回复")  # type: ignore[union-attr]
    t3 = await chat_task(db_session, bot, "c")
    await run(db_engine, t3, FakeRelay("tools_only"))
    assert "调用了 2 次工具" in ((await stream_of(db_session, t3.id)).final_text or "")
    assert (await tasks.get(db_session, t3.id)).status == "succeeded"  # type: ignore[union-attr]


async def test_whitelist_unsupported_help_and_disabled(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    u = User(login_name="vip", display_name="VIP")
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="vip"))
    await db_session.commit()
    bot, _, _ = await seed_bot(db_session, allowed_user_ids=[u.id])
    fake = FakeRelay("normal")
    denied = await chat_task(db_session, bot, "hi", sender="zs")
    await run(db_engine, denied, fake)
    assert (await stream_of(db_session, denied.id)).final_text == msg(
        "no_permission"
    ) and fake.requests == []
    assert (await db_session.execute(select(ChatLog))).scalars().all() == []
    ok = await chat_task(db_session, bot, "hi", sender="vip")
    await run(db_engine, ok, fake)
    assert len(fake.requests) == 1
    # 图片改走下载路径之后，「暂不支持」只剩视频这一类（见 test_chat_handler_media.py）。
    vid = await chat_task(
        db_session,
        bot,
        "",
        sender="vip",
        parts=[{"type": "video", "ref": {"url": "u", "aeskey": "k"}}],
    )
    await run(db_engine, vid, fake)
    assert (await stream_of(db_session, vid.id)).final_text == msg("unsupported_message")
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == vid.id))).scalar_one()
    assert log.message_type == "video" and log.status == "success"
    help_task = await chat_task(db_session, bot, "帮助", sender="vip")
    await run(db_engine, help_task, fake)
    assert (await stream_of(db_session, help_task.id)).final_text == msg("help") and len(
        fake.requests
    ) == 1
    bot.enabled = False
    await db_session.commit()
    off = await chat_task(db_session, bot, "hi", sender="vip")
    await run(db_engine, off, fake)
    row = await tasks.get(db_session, off.id)
    assert row and row.status == "cancelled" and row.error_code == "bot_disabled"
    assert (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == off.id))
    ).scalar_one_or_none() is None


async def test_user_stop_and_superseded(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _, _ = await seed_bot(db_session)
    fake = FakeRelay("slow", chunk_delay=0.25)
    t = await chat_task(db_session, bot, "long")
    ctx = build_ctx(db_engine, t, relay_client_factory=lambda _r: fake.client())
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    await asyncio.sleep(0.9)
    assert await tasks.request_cancel(db_session, t.id, "user_stop") is True
    await db_session.commit()
    await ctx.heartbeat()
    await asyncio.wait_for(runner, 5)
    await ctx.chat_logs.drain(5)
    s = await stream_of(db_session, t.id)
    assert s.is_complete and s.final_text and s.final_text.endswith(msg("task_stopped_suffix"))
    assert fake.aborted == 1
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "cancelled"
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert (
        log.status == "stopped"
        and log.error_message == "用户主动停止"
        and log.output_tokens is None
    )
    old = await chat_task(db_session, bot, "first")
    old_ctx = build_ctx(
        db_engine, old, relay_client_factory=lambda _r: FakeRelay("slow", chunk_delay=0.25).client()
    )
    old_runner = asyncio.create_task(ChatTaskHandler().run(old_ctx))
    await asyncio.sleep(0.6)
    new = await chat_task(db_session, bot, "second")
    new_ctx = build_ctx(
        db_engine, new, relay_client_factory=lambda _r: FakeRelay("normal").client()
    )
    hb = asyncio.create_task(_pump(old_ctx))
    await ChatTaskHandler().run(new_ctx)
    await asyncio.wait_for(old_runner, 6)
    hb.cancel()
    await old_ctx.chat_logs.drain(5)
    s_old = await stream_of(db_session, old.id)
    assert s_old.final_text and s_old.final_text.endswith(msg("superseded_suffix"))
    assert (await tasks.get(db_session, new.id)).status == "succeeded"  # type: ignore[union-attr]


async def _pump(ctx) -> None:  # type: ignore[no-untyped-def]
    while True:
        await asyncio.sleep(0.2)
        await ctx.heartbeat()


async def test_reaped_task_is_not_finished_twice(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """库抖动 → reaper 判这个 worker 失联收了尸，而 worker 其实还活着。

    它必须听心跳的话立刻停手：中止 relay（别再烧算力）、不把任务改回 succeeded、不改写
    reaper 已经写好的终稿（那条没人会再推给用户）、也不补第二条 chat_log。
    """
    bot, _, _ = await seed_bot(db_session)
    fake = FakeRelay("slow", chunk_delay=0.25)
    t = await chat_task(db_session, bot, "会被收尸的长任务")
    tid = t.id
    ctx = build_ctx(db_engine, t, relay_client_factory=lambda _r: fake.client())
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        for _ in range(200):  # 等 relay 真的开始吐帧，再模拟 reaper 在流式进行中收尸
            if fake.requests:
                break
            await asyncio.sleep(0.05)
        assert fake.requests
        await tasks.finish(
            db_session, tid, status="failed", error_code="worker_lost", error_message="收尸"
        )
        await streams.complete(db_session, tid, final_text=msg("worker_lost"))
        await db_session.commit()
        await ctx.heartbeat()  # reaper 收尸后的第一次心跳：应当就地取消
        await asyncio.wait_for(runner, 10)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    db_session.expire_all()
    assert fake.aborted == 1
    row = await tasks.get(db_session, tid)
    assert row and row.status == "failed" and row.error_code == "worker_lost"
    s = await stream_of(db_session, tid)
    assert s.final_text == msg("worker_lost")
    assert (
        await db_session.execute(select(ChatLog).where(ChatLog.task_id == tid))
    ).scalars().all() == []
    assert (await db_session.execute(select(OutboxItem))).scalars().all() == []


async def test_long_task_does_not_send_a_premature_reminder(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    t = await chat_task(db_session, bot, "hi")
    now = [1000.0]
    ctx = build_ctx(
        db_engine,
        t,
        relay_client_factory=lambda _r: FakeRelay("normal").client(),
        clock=lambda: now[0],
    )
    handler = ChatTaskHandler()
    handler._on_first_event = lambda: now.__setitem__(0, 1075.0)  # 首事件后把时钟拨到 75 秒
    await handler.run(ctx)
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert items == []  # Completion travels with the actual final result, never ahead of it.
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded"


async def test_allowlist_and_commands_come_before_relay_check(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row, _ = await seed_bot(db_session)
    relay_row.is_active = False
    stranger = User(login_name="stranger", display_name="路人")
    db_session.add(stranger)
    await db_session.flush()
    db_session.add(BotAllowedUser(bot_id=bot.id, user_id=stranger.id))
    await db_session.commit()
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "你好", sender="nobody")
    await run(db_engine, t, fake)
    s = await stream_of(db_session, t.id)
    assert s.final_text == msg("no_permission")  # 而不是 relay 错误
    assert (await db_session.execute(select(ChatLog))).scalars().all() == []
    db_session.add(UserIdentity(user_id=stranger.id, platform="wecom", platform_user_id="nobody"))
    await db_session.commit()
    t2 = await chat_task(db_session, bot, "help", sender="nobody")
    await run(db_engine, t2, fake)
    assert (await stream_of(db_session, t2.id)).final_text == msg("help")  # 命令不需要 relay
    t3 = await chat_task(db_session, bot, "你好", sender="nobody")
    await run(db_engine, t3, fake)
    assert (await stream_of(db_session, t3.id)).final_text == msg("relay_error", relay="未配置")
