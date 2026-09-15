"""AskUserQuestion 提问轮、自由文本答案与取消（spec §8.2 步骤 5、§8.4）。"""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import streams, tasks
from coreman.core.chat import interactions
from coreman.core.db.models import ChatLog, ChatSession, InteractionState, OutboxItem, Task
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import question_brief
from coreman.runtime.worker.background import Timing
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import build_ctx, seed_bot

Q = {
    "question": "用哪个？",
    "header": "h",
    "options": [{"label": "A", "description": ""}],
    "multiSelect": False,
}


def stepping_clock(step: float = 0.5) -> Callable[[], float]:
    """每取一次就前进固定步长：切后台只取决于调用次数，与真实时钟和帧间隔无关。"""
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += step
        return now

    return clock


async def test_question_round_creates_state_card_and_log(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row, _ = await seed_bot(db_session)
    fake = FakeRelay("ask_user")
    t = await chat_task(db_session, bot, "帮我选")
    await run(db_engine, t, fake)
    s = await stream_of(db_session, t.id)
    assert s.is_complete and s.final_text.endswith("\n\n" + question_brief(Q, index=0, total=1))
    chat_session = (await db_session.execute(select(ChatSession))).scalar_one()
    link = f"{relay_row.relay_url}/session/{chat_session.relay_session_id}"
    assert s.final_text.startswith(msg("session_link_prefix", url=link))
    card = s.pending_card
    assert card and card["card_type"] == "vote_interaction"
    assert card["task_id"].startswith("choice@sales_bot@zs@")
    assert card["task_id"].endswith("@0")
    assert card["checkbox"]["option_list"][-1]["id"] == "opt_other"
    st = await interactions.get_open(
        db_session, kind="choice", scope_key=interactions.choice_scope(bot.id, "zs")
    )
    assert st is not None and st.task_id_prefix == card["task_id"][:-2]
    assert st.state["questions"] == [Q] and st.state["current_index"] == 0
    assert st.state["answers"] == [] and st.state["waiting_for_text"] is False
    ctx = st.state["context"]
    assert ctx["stream_id"] == s.stream_id and ctx["relay_server_id"] == str(relay_row.id)
    assert ctx["chat_id"] == "zs" and ctx["chat_type"] == "single"
    assert ctx["system_prompt"] and "env" not in ctx and "credentials" not in ctx
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded"
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.status == "ask_user" and log.response_content == s.final_text
    # 提问后的普通消息不被吞：没有点「其他」就照常进 AI。
    t2 = await chat_task(db_session, bot, "顺便问下")
    await run(db_engine, t2, FakeRelay("normal"))
    assert (await stream_of(db_session, t2.id)).final_text.endswith(msg("done_suffix"))


async def test_free_text_answer_next_question_and_submit(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    two = [Q, {**Q, "question": "第二题", "header": ""}]
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "questions": two,
            "answers": [],
            "current_index": 0,
            "waiting_for_text": True,
            "waiting_since": "2999-01-01T00:00:00+00:00",
            "context": {"chat_id": "zs", "chat_type": "single"},
        },
        task_id_prefix="choice@sales_bot@zs@1x",
    )
    await db_session.commit()
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "我要 B 方案")
    await run(db_engine, t, fake)
    assert (await stream_of(db_session, t.id)).final_text == msg(
        "answer_recorded", answer="我要 B 方案"
    )
    assert fake.requests == []
    await db_session.refresh(st)
    assert st.state["answers"] == ["我要 B 方案"] and st.state["current_index"] == 1
    assert st.state["waiting_for_text"] is False
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert [i.dedupe_key for i in items] == [f"{st.id}:q1:brief", f"{st.id}:q1:card"]
    assert items[0].payload == {"markdown": question_brief(two[1], index=1, total=2)}
    assert items[1].payload["card"]["task_id"] == "choice@sales_bot@zs@1x@1"
    # 第二题也用文字答：提交。
    await interactions.patch_state(
        db_session, st.id, {"waiting_for_text": True, "waiting_since": "2999-01-01T00:00:00+00:00"}
    )
    await db_session.commit()
    t2 = await chat_task(db_session, bot, "选 A")
    await run(db_engine, t2, fake)
    await db_session.refresh(st)
    assert st.state["answers"] == ["我要 B 方案", "选 A"] and st.status == "open"
    items = (await db_session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert items[-1].payload == {"markdown": msg("choice_generating")}
    submits = await db_session.execute(select(Task).where(Task.kind == "choice_submit"))
    submit = submits.scalar_one()
    assert submit.lane == "normal" and submit.dedupe_key == f"choice_submit:{st.id}"
    assert submit.payload["state_id"] == str(st.id) and submit.inbound_event_id is not None


async def test_cancel_and_expired_wait(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "questions": [Q],
            "answers": [],
            "current_index": 0,
            "waiting_for_text": True,
            "waiting_since": "2000-01-01T00:00:00+00:00",
            "context": {},
        },
    )
    await db_session.commit()
    fake = FakeRelay("normal")
    t = await chat_task(db_session, bot, "等太久了，正常聊天")
    await run(db_engine, t, fake)  # 等待超过 1800 秒：放行为普通消息
    assert len(fake.requests) == 1
    await db_session.refresh(st)
    assert st.state["waiting_for_text"] is False
    t2 = await chat_task(db_session, bot, "取消")
    await run(db_engine, t2, fake)
    assert (await stream_of(db_session, t2.id)).final_text == msg("choice_cancelled")
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    assert len(fake.requests) == 1


async def test_proactive_completion_queues_the_card_itself(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    await db_session.commit()
    fake = FakeRelay("ask_user")
    t = await chat_task(db_session, bot, "慢慢选")
    ctx = build_ctx(
        db_engine, t, relay_client_factory=lambda _r: fake.client(), clock=stepping_clock()
    )
    ctx.settings_store.invalidate()
    handler = ChatTaskHandler(
        timing=Timing(
            wecom_background_after=1,
            pre_warning=0.2,
            bg_min_interval=0.1,
            bg_max_wait=0.2,
            hard_ttl=30,
        )
    )
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)
    s = await streams.get(db_session, t.id)
    assert s and s.delivery_mode == "proactive" and s.pending_card
    rows = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert f"{t.id}:card:0" in {i.dedupe_key for i in rows}
    card_item = next(i for i in rows if i.dedupe_key == f"{t.id}:card:0")
    assert card_item.payload["card"] == s.pending_card
