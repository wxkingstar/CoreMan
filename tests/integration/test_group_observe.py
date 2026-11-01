"""群聊旁听：入站记账、防抖、判断前的规则与插话回合入队。判断服务一律打桩。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update

from coreman.core.chat import group_observe
from coreman.core.chat.decider import Decision
from coreman.core.db.models import (
    Bot,
    GroupObserveDecision,
    GroupObserveState,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.group_observe import GroupObserveHandler
from tests.integration.worker_helpers import build_ctx, seed_bot

CHAT = "oc_group"


class FakeDecider:
    def __init__(self, probability: float | None = 0.9):
        self.probability = probability
        self.calls: list[dict] = []

    async def should_interject(self, state):
        self.calls.append(state)
        return Decision(probability=self.probability, model="fake", latency_ms=5, input_tokens=10)


async def observing_bot(session, mode="on"):
    bot, _, _ = await seed_bot(session)
    bot.platform = "feishu"
    bot.group_observe = mode
    bot.group_observe_scope = "负责查询销售和订单数据"
    await session.commit()
    return bot


async def say(
    session, bot, mid, text, *, sender_type="user", mentions_bot=False, user="u1", mentions=None
):
    raw = {
        "event": {
            "message": {
                "message_id": mid,
                "chat_id": CHAT,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": mentions or [],
            }
        }
    }
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="group",
        chat_id=CHAT,
        sender={
            "platform_user_id": user if sender_type == "user" else "",
            "open_id": f"ou_{user}",
            "sender_type": sender_type,
        },
        message_id=mid,
        mentions_bot=mentions_bot,
        parts=[{"type": "text", "text": text}],
        raw=raw,
        reply_context={"chat_id": CHAT, "chat_type": "group", "message_id": mid},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None, observe=True)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    return task


async def tasks_of(session, bot, kind):
    rows = await session.scalars(
        select(Task).where(Task.bot_id == bot.id, Task.kind == kind).order_by(Task.id)
    )
    return list(rows)


async def quiet_down(session, bot, seconds=30):
    """把这一批消息挪到过去：群里已经安静了一会儿。"""
    past = datetime.now(UTC) - timedelta(seconds=seconds)
    await session.execute(
        update(GroupObserveState)
        .where(GroupObserveState.bot_id == bot.id)
        .values(pending_since=past, last_message_at=past)
    )
    await session.commit()


async def run_observe(db_engine, session, task, decider):
    await session.execute(update(Task).where(Task.id == task.id).values(status="running"))
    await session.commit()
    task = await session.get(Task, task.id, populate_existing=True)
    await GroupObserveHandler(decider).run(build_ctx(db_engine, task))
    return await session.get(Task, task.id, populate_existing=True)


async def test_unmentioned_message_queues_a_delayed_judgement_not_a_turn(db_session):
    bot = await observing_bot(db_session)
    assert await say(db_session, bot, "om1", "谁能拉一下上周的 GMV？") is None
    assert await tasks_of(db_session, bot, "chat") == []
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    assert observe.session_key == group_observe.task_session_key(CHAT)
    assert observe.run_after > datetime.now(UTC)
    state = await db_session.get(GroupObserveState, (bot.id, CHAT))
    assert state.pending_since is not None and state.bot_streak == 0


async def test_mention_is_a_normal_turn_and_clears_pending(db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "大家好")
    turn = await say(db_session, bot, "om2", "@销售 昨天订单多少", mentions_bot=True)
    assert turn is not None and turn.kind == "chat"
    state = await db_session.get(GroupObserveState, (bot.id, CHAT), populate_existing=True)
    assert state.pending_since is None


async def test_bot_messages_count_until_a_human_speaks(db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "通知一", sender_type="bot", user="b")
    await say(db_session, bot, "om2", "通知二", sender_type="bot", user="b")
    state = await db_session.get(GroupObserveState, (bot.id, CHAT), populate_existing=True)
    assert state.bot_streak == 2
    await say(db_session, bot, "om3", "收到")
    await db_session.refresh(state)
    assert state.bot_streak == 0


async def test_bots_mentioning_someone_else_never_trigger_a_judgement(db_session):
    """AI 员工之间求助、答复的 @ 消息只记上下文，旁听的第三个机器人不插话，也不吞掉人的待判断。"""
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "谁能拉一下上周的 GMV？")
    pending = (await db_session.get(GroupObserveState, (bot.id, CHAT))).pending_since
    peer = [{"key": "@_user_1", "id": {"open_id": "ou_other_bot"}, "name": "库存助手"}]
    await say(
        db_session, bot, "om2", "@库存助手 请核对库存", sender_type="bot", user="a", mentions=peer
    )
    assert len(await tasks_of(db_session, bot, "group_observe")) == 1
    state = await db_session.get(GroupObserveState, (bot.id, CHAT), populate_existing=True)
    assert state.pending_since == pending and state.bot_streak == 1


async def test_transcript_names_people_by_user_id_not_app_scoped_open_id(db_session):
    bot = await observing_bot(db_session)
    person = User(login_name="lilei", display_name="李雷")
    db_session.add(person)
    await db_session.flush()
    # 通讯录同步应用的 open_id 与机器人应用收到的不同；user_id 全企业一致。
    db_session.add(
        UserIdentity(
            user_id=person.id, platform="feishu", platform_user_id="u1", open_id="ou_sync_app"
        )
    )
    await db_session.commit()
    await say(db_session, bot, "om1", "小数在吗")
    await say(db_session, bot, "om2", "我也在", user="u2")
    lines = await group_observe.transcript(db_session, bot, CHAT)
    assert [line.sender for line in lines] == ["李雷", "同事 1"]


async def test_only_the_latest_message_judges(db_engine, db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "上周退货率多少？")
    await say(db_session, bot, "om2", "有人知道吗")
    first, second = await tasks_of(db_session, bot, "group_observe")
    decider = FakeDecider()
    done = await run_observe(db_engine, db_session, first, decider)
    assert done.result == {"skipped": "newer_message"} and decider.calls == []
    # 最新那条还没安静够：放回队列再等。
    again = await run_observe(db_engine, db_session, second, decider)
    assert again.status == "queued" and decider.calls == []


async def test_judges_and_queues_an_interjection_turn(db_engine, db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "上周退货率多少？有人知道吗")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    decider = FakeDecider(0.9)
    done = await run_observe(db_engine, db_session, observe, decider)
    assert done.status == "succeeded"
    (state,) = decider.calls
    assert state["bot"]["role"] == "负责查询销售和订单数据"
    assert state["recent_messages"][-1]["text"] == "上周退货率多少？有人知道吗"
    (turn,) = await tasks_of(db_session, bot, "chat")
    assert turn.session_key == CHAT and turn.payload["origin"] == "observe"
    decision = await db_session.scalar(select(GroupObserveDecision))
    assert decision.outcome == "interject" and decision.task_id == turn.id
    assert decision.probability == 0.9


async def test_shadow_mode_records_but_never_speaks(db_engine, db_session):
    bot = await observing_bot(db_session, mode="shadow")
    await say(db_session, bot, "om1", "上周退货率多少？")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    await run_observe(db_engine, db_session, observe, FakeDecider(0.9))
    assert await tasks_of(db_session, bot, "chat") == []
    decision = await db_session.scalar(select(GroupObserveDecision))
    assert decision.mode == "shadow" and decision.outcome == "interject"


async def test_below_threshold_stays_quiet(db_engine, db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "哈哈哈")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    await run_observe(db_engine, db_session, observe, FakeDecider(0.05))
    assert await tasks_of(db_session, bot, "chat") == []
    decision = await db_session.scalar(select(GroupObserveDecision))
    assert decision.outcome == "below_threshold"


async def test_bot_streak_and_mute_skip_the_model(db_engine, db_session):
    bot = await observing_bot(db_session)
    for i in range(6):
        await say(db_session, bot, f"om{i}", f"机器人消息 {i}", sender_type="bot", user="b")
    observe = (await tasks_of(db_session, bot, "group_observe"))[-1]
    await quiet_down(db_session, bot)
    decider = FakeDecider()
    done = await run_observe(db_engine, db_session, observe, decider)
    assert done.result == {"skipped": "bot_streak"} and decider.calls == []

    await say(db_session, bot, "om9", "上周退货率多少？")
    await group_observe.set_muted(db_session, bot.id, CHAT, True)
    await db_session.execute(
        update(GroupObserveState)
        .where(GroupObserveState.bot_id == bot.id)
        .values(pending_since=datetime.now(UTC) - timedelta(seconds=30))
    )
    await db_session.commit()
    # 静音清掉了待判断：这条消息之后的任务什么都不做。
    observe = (await tasks_of(db_session, bot, "group_observe"))[-1]
    await run_observe(db_engine, db_session, observe, decider)
    assert decider.calls == []


async def test_busy_session_waits_for_the_reply(db_engine, db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "@销售 昨天订单多少", mentions_bot=True)
    await say(db_session, bot, "om2", "顺便问下退货率")
    observe = (await tasks_of(db_session, bot, "group_observe"))[-1]
    await quiet_down(db_session, bot)
    decider = FakeDecider()
    done = await run_observe(db_engine, db_session, observe, decider)
    assert done.status == "queued" and decider.calls == []


def record_schedule_configure(monkeypatch):
    """记下本轮有没有挂定时任务能力（插话回合只答不做，不该挂）。"""
    from coreman.runtime.worker.chat import schedules

    calls: list[str] = []

    async def configure(session, ctx, intake, relay_session_id, extra, env):
        calls.append("schedules")
        return extra, env

    monkeypatch.setattr(schedules, "configure", configure)
    return calls


async def _interjection_turn(
    db_engine, db_session, scenario, monkeypatch, *, interjected_before=False
):
    from coreman.core.bus import instances, tasks
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes import fake_relay

    monkeypatch.setitem(
        fake_relay.SCENARIOS,
        "observe_silent",
        lambda: [
            fake_relay._text(group_observe.NO_REPLY),
            fake_relay.FINISH,
            fake_relay._chunk(None, usage=fake_relay.USAGE),
            fake_relay.DONE,
        ],
    )
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om0", "今天中午吃什么", user="u2")
    await say(db_session, bot, "om1", "上周退货率多少？有人知道吗")
    observe = (await tasks_of(db_session, bot, "group_observe"))[-1]
    await quiet_down(db_session, bot)
    await run_observe(db_engine, db_session, observe, FakeDecider(0.9))
    if interjected_before:
        await db_session.execute(
            update(GroupObserveState)
            .where(GroupObserveState.bot_id == bot.id)
            .values(last_interjected_at=datetime.now(UTC) - timedelta(days=1))
        )
    await instances.register(
        db_session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    # 新事务再认领：now() 取事务开始时间，旧事务看到的 now() 早于插话回合的 run_after。
    await db_session.commit()
    turn = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert turn is not None and turn.payload["origin"] == "observe"
    relay = fake_relay.FakeRelay(scenario)
    ctx = build_ctx(db_engine, turn, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    from coreman.core.db.models import TaskStream

    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == turn.id))
    decision = await db_session.scalar(select(GroupObserveDecision))
    await db_session.refresh(decision)
    state = await db_session.get(GroupObserveState, (bot.id, CHAT), populate_existing=True)
    return relay.requests[0], stream, decision, state


async def test_interjection_turn_sees_the_group_and_may_stay_silent(
    db_engine, db_session, monkeypatch
):
    configured = record_schedule_configure(monkeypatch)
    body, stream, decision, state = await _interjection_turn(
        db_engine, db_session, "observe_silent", monkeypatch
    )
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "所在的话题" in turn and "今天中午吃什么" in turn
    # 只答不做：插话回合不挂定时任务、个人凭证这些有后果的能力。
    assert "只答不做" in turn
    assert configured == []
    # 本轮要回应的消息只在正文出现一次，不在记录里重复。
    assert turn.count("上周退货率多少？有人知道吗") == 1
    assert group_observe.NO_REPLY in turn  # 插话规则在本轮块里
    assert stream.reply_context["_observe"] is True
    assert stream.final_text == group_observe.NO_REPLY
    assert decision.outcome == "vetoed"
    assert state.last_interjected_at is None and state.bot_streak == 0


async def test_interjection_turn_that_speaks_counts_as_a_bot_message(
    db_engine, db_session, monkeypatch
):
    _, stream, decision, state = await _interjection_turn(
        db_engine, db_session, "normal", monkeypatch
    )
    # 主动插话不带「✅ 回复已生成」；在这个群第一次主动说话，末尾附上旁听说明。
    assert stream.final_text == "你好，世界\n\n。\n\n" + msg("observe_intro", name="销售")
    assert decision.outcome == "sent"
    assert state.last_interjected_at is not None and state.bot_streak == 1


async def another_observing_bot(session, first, key, name):
    """同一个群里再开一个旁听的机器人（同一位创建者、同一个运行时）。"""
    bot = Bot(
        bot_key=key,
        platform="feishu",
        name=name,
        created_by=first.created_by,
        relay_server_id=first.relay_server_id,
        model=first.model,
        working_dir=f"/data/skills/{key}",
        system_prompt=f"你是{name}",
        merged_system_prompt=f"你是{name}",
        credentials_enc=first.credentials_enc,
        env_vars_enc=first.env_vars_enc,
        group_observe="on",
        group_observe_scope=f"负责{name}",
    )
    session.add(bot)
    await session.commit()
    return bot


async def judge(db_engine, session, bot, probability):
    (observe,) = await tasks_of(session, bot, "group_observe")
    await run_observe(db_engine, session, observe, FakeDecider(probability))
    rows = await tasks_of(session, bot, "group_observe")
    return [task for task in rows if task.payload.get("decision_id")]


async def test_several_bots_wanting_to_speak_leave_it_to_the_most_suitable(db_engine, db_session):
    sales = await observing_bot(db_session)
    ops = await another_observing_bot(db_session, sales, "ops_bot", "运维")
    for bot in (sales, ops):
        await say(db_session, bot, "om1", "上周退货率多少？有人知道吗")
        await quiet_down(db_session, bot)
    (sales_pick,) = await judge(db_engine, db_session, sales, 0.6)
    (ops_pick,) = await judge(db_engine, db_session, ops, 0.9)
    # 还没选出谁接话：谁都没进 agent 回合。
    assert await tasks_of(db_session, sales, "chat") == []
    assert await tasks_of(db_session, ops, "chat") == []
    await run_observe(db_engine, db_session, sales_pick, None)
    await run_observe(db_engine, db_session, ops_pick, None)
    assert await tasks_of(db_session, sales, "chat") == []
    (turn,) = await tasks_of(db_session, ops, "chat")
    rows = {row.bot_id: row for row in await db_session.scalars(select(GroupObserveDecision))}
    assert rows[sales.id].outcome == "yielded" and rows[sales.id].task_id is None
    assert rows[ops.id].outcome == "interject" and rows[ops.id].task_id == turn.id


async def test_a_late_judgement_yields_once_another_bot_has_taken_the_message(
    db_engine, db_session
):
    sales = await observing_bot(db_session)
    ops = await another_observing_bot(db_session, sales, "ops_bot", "运维")
    for bot in (sales, ops):
        await say(db_session, bot, "om1", "上周退货率多少？有人知道吗")
        await quiet_down(db_session, bot)
    (sales_pick,) = await judge(db_engine, db_session, sales, 0.6)
    await run_observe(db_engine, db_session, sales_pick, None)
    assert len(await tasks_of(db_session, sales, "chat")) == 1
    # 运维判断得晚，概率再高也不再插一句。
    (ops_pick,) = await judge(db_engine, db_session, ops, 0.95)
    await run_observe(db_engine, db_session, ops_pick, None)
    assert await tasks_of(db_session, ops, "chat") == []
    row = await db_session.scalar(
        select(GroupObserveDecision).where(GroupObserveDecision.bot_id == ops.id)
    )
    assert row.outcome == "yielded"


async def test_mention_turn_reads_the_transcript_as_background_only(
    db_engine, db_session, monkeypatch
):
    from coreman.core.bus import instances, tasks
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes import fake_relay

    configured = record_schedule_configure(monkeypatch)
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "上周退货率多少？", user="u2")
    await say(db_session, bot, "om2", "帮我查下昨天订单量", mentions_bot=True)
    await instances.register(
        db_session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    await db_session.commit()
    turn = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert turn is not None and turn.kind == "chat"
    relay = fake_relay.FakeRelay("normal")
    ctx = build_ctx(db_engine, turn, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    content = relay.requests[0]["messages"][-1]["content"]
    content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    assert "只作背景参考" in content and "不是本轮任务" in content
    assert "上周退货率多少？" in content
    assert content.count("帮我查下昨天订单量") == 1
    assert group_observe.NO_REPLY not in content  # @ 它的回合没有插话规则
    assert configured == ["schedules"]  # @ 回合照常挂定时任务等能力


async def test_a_slow_interjection_says_it_is_looking_then_answers(
    db_engine, db_session, monkeypatch
):
    monkeypatch.setattr(group_observe, "INTERJECT_ACK_SECONDS", 0)
    _, stream, decision, _ = await _interjection_turn(db_engine, db_session, "normal", monkeypatch)
    ack = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert ack.payload == {"text": msg("observe_ack"), "_reply": True}
    assert ack.target == {"chat_id": CHAT, "message_id": "om1"}
    assert stream.final_text.startswith("你好，世界") and decision.outcome == "sent"


async def test_an_interjection_that_runs_too_long_claims_the_work_instead(
    db_engine, db_session, monkeypatch
):
    monkeypatch.setattr(group_observe, "INTERJECT_LIMIT_SECONDS", 0)
    _, stream, _, state = await _interjection_turn(db_engine, db_session, "normal", monkeypatch)
    assert stream.final_text.startswith(msg("observe_timeout"))
    assert state.bot_streak == 1


async def test_later_interjections_in_the_same_group_skip_the_intro(
    db_engine, db_session, monkeypatch
):
    _, stream, _, _ = await _interjection_turn(
        db_engine, db_session, "normal", monkeypatch, interjected_before=True
    )
    assert stream.final_text == "你好，世界\n\n。"


async def test_another_persons_mention_waits_instead_of_stopping_the_current_task(db_session):
    bot = await observing_bot(db_session)
    first = await say(db_session, bot, "om1", "帮我导出上周订单", mentions_bot=True, user="u1")
    assert not first.payload.get("serialize_session")
    second = await say(db_session, bot, "om2", "帮我查下库存", mentions_bot=True, user="u2")
    assert second.payload["serialize_session"] is True
    notice = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert notice.payload == {"text": msg("group_queued"), "_reply": True}
    assert notice.target == {"chat_id": CHAT, "message_id": "om2"}
    # 同一个人接着发：仍按原样替换自己上一条（这里还有别人的任务在排，所以一起排队）。
    third = await say(db_session, bot, "om3", "改成导出本周", mentions_bot=True, user="u1")
    assert third.payload["serialize_session"] is True


async def test_the_same_persons_follow_up_still_replaces_their_own_task(db_session):
    bot = await observing_bot(db_session)
    await say(db_session, bot, "om1", "帮我导出上周订单", mentions_bot=True, user="u1")
    again = await say(db_session, bot, "om2", "改成导出本周", mentions_bot=True, user="u1")
    assert not again.payload.get("serialize_session")
    assert await db_session.scalar(select(OutboxItem)) is None
