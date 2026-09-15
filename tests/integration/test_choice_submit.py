"""choice_submit 提交轮：答案续跑同一会话、从创建即主动推送、嵌套提问。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import interactions
from coreman.core.db.models import (
    ChatLog,
    InboundEvent,
    InteractionState,
    OutboxItem,
    RelayServer,
    TaskStream,
)
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import format_answers
from coreman.runtime.worker.background import Timing
from coreman.runtime.worker.choice_submit import ChoiceSubmitHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.worker_helpers import build_ctx, seed_bot

Q = {
    "question": "用哪个？",
    "header": "h",
    "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
    "multiSelect": False,
}
FAST = Timing(bg_min_interval=0.0, bg_max_wait=0.05, silence_marks=(0.3, 0.6))


async def _submitted_state(session: AsyncSession, bot, relay: RelayServer, *, user="zs"):  # type: ignore[no-untyped-def]
    rs = uuid.uuid4()
    st = await interactions.open_state(
        session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, user),
        state={
            "questions": [Q],
            "answers": ["B"],
            "current_index": 1,
            "waiting_for_text": False,
            "waiting_since": None,
            "context": {
                "relay_session_id": str(rs),
                "stream_id": "bot:sales_bot|user:zs|ts:1|rnd:abc123",
                "relay_server_id": str(relay.id),
                "model": bot.model,
                "working_dir": bot.working_dir,
                "backend": "claude",
                "system_prompt": "SNAPSHOT PROMPT",
                "chat_type": "single",
                "chat_id": user,
                "session_key": user,
                "user_id": None,
                "platform_user_id": user,
                "sse_timeout_seconds": 3600,
                "verbosity_level": 1,
                "effort_level": None,
            },
        },
        task_id_prefix=f"choice@sales_bot@{user}@1x",
    )
    await session.commit()
    return st, rs


async def _submit_task(session: AsyncSession, bot, st, *, user="zs"):  # type: ignore[no-untyped-def]
    # 提交任务总是带着触发它的入站事件（文本答案或卡片事件），这里造一个卡片事件充当。
    ev = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=f"e-{uuid.uuid4()}",
        kind="card_action",
        chat_type="single",
        chat_id=user,
        sender_platform_user_id=user,
        payload={},
        reply_context={
            "gateway_instance": "gw",
            "req_id": "used-by-card-update",
            "chat_type": "single",
            "chat_id": user,
        },
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="choice_submit",
            payload={
                "state_id": str(st.id),
                "bot_key": bot.bot_key,
                "platform_user_id": user,
                "chat_type": "single",
                "chat_id": user,
            },
            session_key=user,
            inbound_event_id=ev.id,
            dedupe_key=f"choice_submit:{st.id}",
        ),
    )
    await session.commit()
    assert t
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed
    return claimed


async def _run(engine, task, relay, timing=FAST):  # type: ignore[no-untyped-def]
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChoiceSubmitHandler(timing=timing).run(ctx)
    await ctx.chat_logs.drain(5)
    return ctx


async def test_submit_round_continues_the_session_and_pushes_segments(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    st, rs = await _submitted_state(db_session, bot, relay)
    fake = FakeRelay("submit_round")
    t = await _submit_task(db_session, bot, st)
    # 这一条断言的是「分段按工具边界切」，不能同时依赖调度时序：`FAST` 的 bg_max_wait=0.05
    # 会在负载高时把第三帧当进展先推走，finish 的差量就空了；silence_marks 也可能先插一条
    # 心跳顶掉 texts[0]。把 max_wait 放到 60 秒、心跳交回生产值（90/240/420/600），
    # 于是只有工具边界会触发推送，与机器快慢无关。
    await _run(
        db_engine,
        t,
        fake,
        timing=Timing(bg_min_interval=0.0, bg_max_wait=60.0, silence_marks=()),
    )
    body = fake.requests[0]
    assert body["session_id"] == str(rs)
    # 延续会话，但不得把快照身份当成当前用户；未映射的 zs 必须明确未知。
    assert "SNAPSHOT PROMPT" not in body["messages"][0]["content"]
    assert "identity_unknown" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == format_answers([Q], ["B"])
    assert body["env_vars"]["COREMAN_SESSION_ID"] == str(rs)
    assert body["env_vars"]["COREMAN_PLATFORM_USER_ID"] == "zs"
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == t.id))
    ).scalar_one()
    assert stream.delivery_mode == "proactive"
    assert stream.stream_id == "bot:sales_bot|user:zs|ts:1|rnd:abc123|submit"
    assert stream.is_complete and (stream.reply_context.get("req_id") or "") == ""
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    texts = [i.payload["markdown"] for i in items]
    assert texts[0] == msg("bg_progress_prefix") + "先查数据"
    assert texts[-1].startswith(msg("bg_done_prefix")) and "结论：选 B" in texts[-1]
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.message_type == "ask_user_answer" and log.message_content == format_answers(
        [Q], ["B"]
    )
    assert log.stream_id == stream.stream_id and log.status == "success"
    assert log.relay_session_id == rs
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []


async def test_duplicate_and_config_changed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    st, _rs = await _submitted_state(db_session, bot, relay)
    await interactions.set_status(db_session, st.id, "submitted")
    await db_session.commit()
    t = await _submit_task(db_session, bot, st)
    fake = FakeRelay("submit_round")
    await _run(db_engine, t, fake)
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert [i.payload["markdown"] for i in items] == [msg("choice_duplicate")]
    assert fake.requests == []
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded"
    other = RelayServer(name="r-other", host="o.test", clawrelay_port=80, model_provider="claude")
    db_session.add(other)
    await db_session.flush()
    st2, _ = await _submitted_state(db_session, bot, other)  # 快照指向另一台 relay
    t2 = await _submit_task(db_session, bot, st2)
    await _run(db_engine, t2, fake)
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert items[-1].payload["markdown"] == msg("choice_config_changed")
    assert fake.requests == []
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []


async def test_silence_heartbeats_and_empty_reply(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    st, _rs = await _submitted_state(db_session, bot, relay)
    # 1.5 秒 > TICK_SECONDS(1.0)：两次 tick 稳稳早于首字节，不靠零点几毫秒的调度先后。
    fake = FakeRelay("empty", first_byte_delay=1.5)
    t = await _submit_task(db_session, bot, st)
    await _run(db_engine, t, fake)
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    texts = [i.payload["markdown"] for i in items]
    beats = [x for x in texts if x.startswith("⏳ AI 仍在处理中")]
    assert len(beats) == 2
    url = (
        f"http://localhost/api/admin/runtime-nodes/{relay.runtime_node_id}/claude/session/"
        f"{st.state['context']['relay_session_id']}"
    )
    assert texts[-1].endswith(msg("empty_stream", url=url))


async def test_nested_question_rebuilds_state_and_queues_card(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    st, _rs = await _submitted_state(db_session, bot, relay)
    fake = FakeRelay("nested_ask_user")
    t = await _submit_task(db_session, bot, st)
    await _run(db_engine, t, fake)
    rows = (await db_session.execute(select(InteractionState))).scalars().all()
    assert len(rows) == 1 and rows[0].id != st.id and rows[0].status == "open"
    assert rows[0].state["questions"][0]["question"] == "确定吗？"
    assert rows[0].task_id_prefix != "choice@sales_bot@zs@1x"
    keys = {i.dedupe_key for i in (await db_session.execute(select(OutboxItem))).scalars().all()}
    assert f"{t.id}:card:0" in keys
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.status == "ask_user" and log.message_type == "ask_user_answer"


async def test_snapshots_are_kept_per_task_not_on_the_handler(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """WorkerService 把一个处理器实例复用给所有并发任务：两个人的快照不能互相覆盖。"""
    bot, relay, _ = await seed_bot(db_session)
    st_a, rs_a = await _submitted_state(db_session, bot, relay, user="zs")
    st_b, rs_b = await _submitted_state(db_session, bot, relay, user="ls")
    ta = await _submit_task(db_session, bot, st_a, user="zs")
    tb = await _submit_task(db_session, bot, st_b, user="ls")
    handler = ChoiceSubmitHandler(timing=FAST)
    ctx_a, ctx_b = build_ctx(db_engine, ta), build_ctx(db_engine, tb)
    # 两轮的入口依次跑完（并发时就是这个交错顺序），后一轮不得把前一轮的会话冲掉。
    assert await handler._resolve(db_session, ctx_a)
    assert await handler._resolve(db_session, ctx_b)
    await db_session.commit()
    assert handler._snapshot(ctx_a).context["relay_session_id"] == str(rs_a)
    assert handler._snapshot(ctx_b).context["relay_session_id"] == str(rs_b)
    assert handler._snapshot(ctx_a).state_id == st_a.id
