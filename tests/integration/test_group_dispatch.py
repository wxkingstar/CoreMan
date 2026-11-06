"""群聊调度员（第一期试运行）：换判断题、排调度回合、校验决定并记成 shadow 任务，群里不发言。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.chat import decider as decider_mod
from coreman.core.chat import dispatch, group_observe
from coreman.core.db.models import (
    GroupObserveDecision,
    GroupObserveState,
    TaskStream,
    WorkTask,
    WorkTaskEvent,
)
from tests.integration.test_group_observe import (
    CHAT,
    FakeDecider,
    observing_bot,
    quiet_down,
    record_schedule_configure,
    run_observe,
    say,
    tasks_of,
)

STAFF = [
    dispatch.Employee("data_bot", "数据助手", "负责销售和订单数据", uuid.uuid4()),
    dispatch.Employee("ops_bot", "运维助手", "负责服务器和白名单", uuid.uuid4()),
]


async def dispatcher_bot(session, mode="shadow"):
    bot = await observing_bot(session, mode=mode)
    bot.group_role = "dispatcher"
    bot.group_observe_scope = "运营团队的工作群"
    await session.commit()
    return bot


def plan(**data):
    return "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


async def _dispatch_turn(
    db_engine, db_session, monkeypatch, answer: str, mode="shadow", humans: int | None = None
):
    from coreman.core.bus import instances, tasks
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes import fake_relay

    monkeypatch.setitem(
        fake_relay.SCENARIOS,
        "dispatch_plan",
        lambda: [
            fake_relay._text(answer),
            fake_relay.FINISH,
            fake_relay._chunk(None, usage=fake_relay.USAGE),
            fake_relay.DONE,
        ],
    )
    bot = await dispatcher_bot(db_session, mode)
    # 员工都换成真实存在的机器人，任务的执行员工外键才挂得上。
    staff = []
    for item in STAFF:
        other = await another_bot(db_session, bot, item.key, item.name)
        staff.append(dispatch.Employee(item.key, item.name, item.role, other.id))

    async def employees(session, dispatcher, chat_id, requester_id, cipher):
        return staff, []

    async def human_count(bot, chat_id, cipher):
        return humans

    monkeypatch.setattr(dispatch, "employees", employees)
    monkeypatch.setattr(dispatch, "human_count", human_count)
    configured = record_schedule_configure(monkeypatch)
    await say(db_session, bot, "om1", "上周退货率多少？谁帮我查一下")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    decider = FakeDecider(0.9)
    await run_observe(db_engine, db_session, observe, decider)
    # 单人群不问判断模型，直接排调度回合。
    expected = [] if humans == 1 else [decider_mod.DISPATCH_INSTRUCTION]
    assert decider.instructions == expected
    await instances.register(
        db_session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    await db_session.commit()
    turn = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert turn is not None and turn.payload["origin"] == "dispatch"
    assert turn.payload.get("solo", False) is (humans == 1)
    assert turn.session_key == dispatch.session_key(CHAT)
    relay = fake_relay.FakeRelay("dispatch_plan")
    ctx = build_ctx_for(db_engine, turn, relay)
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == turn.id))
    decision = await db_session.scalar(select(GroupObserveDecision))
    await db_session.refresh(decision)
    return bot, relay.requests[0], stream, decision, configured, staff


def build_ctx_for(db_engine, turn, relay):
    from tests.integration.worker_helpers import build_ctx

    return build_ctx(db_engine, turn, relay_client_factory=lambda _r: relay.client())


async def another_bot(session, first, key, name):
    from coreman.core.db.models import Bot

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
    )
    session.add(bot)
    await session.commit()
    return bot


async def test_dispatcher_records_who_it_would_assign_and_stays_silent(
    db_engine, db_session, monkeypatch
):
    answer = plan(
        action="create",
        reason="数据问题",
        tasks=[
            {
                "title": "查上周退货率",
                "assignee": "data_bot",
                "summary": "算出上周退货率并和前一周比较",
                "brief": "口径按退货单数除以签收单数",
                "related": ["T-999"],
            }
        ],
    )
    bot, body, stream, decision, configured, staff = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer
    )
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "`data_bot`（数据助手）" in turn and "只输出一个 JSON 代码块" in turn
    assert configured == []  # 调度员只安排：不挂定时任务这些能力
    assert stream.final_text == group_observe.NO_REPLY  # 群里什么也不发
    assert decision.outcome == "dispatched"
    assert decision.dispatch["plan"]["action"] == "create"
    (task,) = list(await db_session.scalars(select(WorkTask)))
    assert task.status == "shadow" and task.title == "查上周退货率"
    assert task.assignee_bot_id == staff[0].bot_id and task.dispatcher_bot_id == bot.id
    assert task.related_codes == []  # 不认识的编号丢掉
    assert task.brief == "口径按退货单数除以签收单数" and task.decision_id == decision.id
    (event,) = list(await db_session.scalars(select(WorkTaskEvent)))
    assert event.kind == "created" and "数据助手" in event.human_text
    assert decision.dispatch["codes"] == [task.code]


async def test_invalid_dispatch_output_creates_nothing(db_engine, db_session, monkeypatch):
    answer = plan(action="create", tasks=[{"title": "查数据", "assignee": "nobody"}])
    _, _, stream, decision, _, _ = await _dispatch_turn(db_engine, db_session, monkeypatch, answer)
    assert stream.final_text == group_observe.NO_REPLY
    assert decision.outcome == "dispatch_invalid"
    assert decision.dispatch["error"] == "unknown_assignee"
    assert list(await db_session.scalars(select(WorkTask))) == []


async def test_ordinary_observers_keep_the_interjection_question(db_engine, db_session):
    bot = await observing_bot(db_session, mode="shadow")
    await say(db_session, bot, "om1", "上周退货率多少？")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    decider = FakeDecider(0.9)
    await run_observe(db_engine, db_session, observe, decider)
    assert decider.instructions == [decider_mod.INSTRUCTION]
    assert await tasks_of(db_session, bot, "chat") == []  # 普通员工试运行不排回合


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"action": "maybe"}, "bad_action"),
        ({"action": "create", "tasks": []}, "bad_tasks"),
        ({"action": "create", "tasks": [{"assignee": "data_bot"}] * 4}, "bad_tasks"),
        ({"action": "create", "tasks": [{"assignee": "data_bot", "title": ""}]}, "missing_title"),
        ({"action": "append", "task_code": "T-5", "note": "加个渠道"}, "bad_append"),
        ({"action": "clarify"}, "missing_question"),
    ],
)
def test_parse_rejects_bad_decisions(data, error):
    with pytest.raises(ValueError, match=error):
        dispatch.parse(json.dumps(data), STAFF, {"T-1"})


def test_reply_is_only_for_single_person_groups():
    with pytest.raises(ValueError, match="bad_action"):
        dispatch.parse(plan(action="reply", text="不客气"), STAFF, set())
    with pytest.raises(ValueError, match="missing_text"):
        dispatch.parse(plan(action="reply"), STAFF, set(), solo=True)
    replied = dispatch.parse(plan(action="reply", text="不客气" * 200), STAFF, set(), solo=True)
    assert replied.action == "reply" and len(replied.text) == dispatch.REPLY_LIMIT
    note = dispatch.note(STAFF, [], [], {}, solo=True)
    assert "只有一位真人" in note and "reply" in note and "- none" not in note
    assert "reply" not in dispatch.note(STAFF, [], [], {})


def test_parse_accepts_append_clarify_and_trims():
    appended = dispatch.parse(
        plan(action="append", task_code="T-1", note="再加个渠道维度"), STAFF, {"T-1"}
    )
    assert appended.task_code == "T-1" and appended.note == "再加个渠道维度"
    asked = dispatch.parse(plan(action="clarify", question="要看哪个渠道？"), STAFF, set())
    assert asked.question == "要看哪个渠道？"
    long = dispatch.parse(
        plan(action="create", tasks=[{"assignee": "ops_bot", "title": "很" * 50}]), STAFF, set()
    )
    assert len(long.tasks[0]["title"]) == dispatch.TITLE_LIMIT
    with pytest.raises(ValueError, match="not_json"):
        dispatch.parse("不是 JSON", STAFF, set())


@pytest.mark.parametrize(
    ("seen_ago", "skipped"), [(timedelta(0), True), (timedelta(hours=2), False)]
)
async def test_members_leave_groups_with_a_dispatcher_to_it(
    db_engine, db_session, seen_ago, skipped
):
    """群里有调度员（且它也收到了这批消息）时成员不判断；调度员早已不在这个群就照常判断。"""
    member = await observing_bot(db_session)
    other = await another_bot(db_session, member, "dispatcher_bot", "调度员")
    other.group_role, other.group_observe = "dispatcher", "shadow"
    db_session.add(
        GroupObserveState(
            bot_id=other.id, chat_id=CHAT, last_message_at=datetime.now(UTC) - seen_ago
        )
    )
    await db_session.commit()
    await say(db_session, member, "om1", "谁能查一下这个 IP 在不在白名单？")
    (observe,) = await tasks_of(db_session, member, "group_observe")
    await quiet_down(db_session, member)
    decider = FakeDecider(0.9)
    done = await run_observe(db_engine, db_session, observe, decider)
    if skipped:
        assert done.result == {"skipped": "dispatcher"} and decider.calls == []
        state = await db_session.get(GroupObserveState, (member.id, CHAT), populate_existing=True)
        assert state.pending_since is None
    else:
        assert len(decider.calls) == 1


async def _run_work_turn(db_engine, db_session, monkeypatch, assignee_id, answer: str):
    from coreman.core.bus import tasks
    from coreman.core.db.models import Task
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes import fake_relay

    monkeypatch.setitem(
        fake_relay.SCENARIOS,
        "work_answer",
        lambda: [
            fake_relay._text(answer),
            fake_relay.FINISH,
            fake_relay._chunk(None, usage=fake_relay.USAGE),
            fake_relay.DONE,
        ],
    )
    turn = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert turn is not None and turn.bot_id == assignee_id
    relay = fake_relay.FakeRelay("work_answer")
    ctx = build_ctx_for(db_engine, turn, relay)
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == turn.id))
    done = await db_session.get(Task, turn.id, populate_existing=True)
    assert relay.requests, (done.status, done.error_code, done.error_message)
    return done, relay.requests[0], stream


async def test_live_dispatcher_hands_the_task_over_and_the_assignee_reports_back(
    db_engine, db_session, monkeypatch
):
    answer = plan(
        action="create",
        reason="数据问题",
        tasks=[
            {
                "title": "查上周退货率",
                "assignee": "data_bot",
                "summary": "算出上周退货率",
                "brief": "口径按退货单数除以签收单数",
                "track": True,
            }
        ],
    )
    bot, _, stream, decision, _, staff = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer, mode="on"
    )
    (task,) = list(await db_session.scalars(select(WorkTask)))
    assert task.status == "queued"
    assert task.tracked
    link = f"[{task.code}](http://localhost/work-tasks/{task.code})"  # 点开看任务页
    assert stream.final_text == f"@数据助手 查上周退货率（{link}）"  # 群里回一行
    assert decision.dispatch["said"] == stream.final_text
    from coreman.core.db.models import Task

    (handoff,) = list(await db_session.scalars(select(Task).where(Task.bot_id == staff[0].bot_id)))
    assert handoff.session_key == dispatch.work_session_key(task.code)
    assert handoff.payload["work_task_id"] == task.id
    assert handoff.inbound_event_id == task.origin_event_id

    done, body, reply = await _run_work_turn(
        db_engine, db_session, monkeypatch, staff[0].bot_id, f"{link} 完成：上周退货率 4.2%"
    )
    assert done.status == "succeeded"
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "口径按退货单数除以签收单数" in turn and f"「{link} 完成：」" in turn
    assert reply.reply_context["message_id"] == "om1"  # 回复发起人的原消息
    await db_session.refresh(task)
    assert task.status == "done" and task.summary == f"{task.code} 完成：上周退货率 4.2%"
    kinds = [
        e.kind
        for e in await db_session.scalars(
            select(WorkTaskEvent)
            .where(WorkTaskEvent.work_task_id == task.id)
            .order_by(WorkTaskEvent.id)
        )
    ]
    assert kinds == ["created", "started", "done"]


async def test_simple_requests_say_who_took_it_without_a_task_code(
    db_engine, db_session, monkeypatch
):
    answer = plan(
        action="create",
        reason="问办法",
        tasks=[{"title": "说明白名单办理方式", "assignee": "ops_bot", "summary": "说明找谁"}],
    )
    _, _, stream, decision, _, staff = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer, mode="on"
    )
    (task,) = list(await db_session.scalars(select(WorkTask)))
    assert not task.tracked and task.status == "queued"  # 没写 track：照样交接、记下
    assert stream.final_text == "交给 @运维助手 了：说明白名单办理方式"  # 说交给谁，不带编号
    assert task.code not in stream.final_text
    assert decision.dispatch["said"] == stream.final_text

    done, body, _ = await _run_work_turn(
        db_engine, db_session, monkeypatch, staff[1].bot_id, "找运维开通，发来页面上的 IP 即可。"
    )
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "不要写任务编号" in turn and task.code not in turn
    await db_session.refresh(task)
    assert task.status == "done" and task.summary == "找运维开通，发来页面上的 IP 即可。"


async def test_live_dispatcher_asks_in_the_group_when_unclear(db_engine, db_session, monkeypatch):
    answer = plan(action="clarify", reason="不清楚", question="要看线上还是门店？")
    _, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer, mode="on"
    )
    assert stream.final_text == "要看线上还是门店？"
    assert decision.outcome == "dispatch_clarify"


async def test_single_person_group_gets_a_direct_reply_without_judging(
    db_engine, db_session, monkeypatch
):
    answer = plan(action="reply", reason="致谢", text="不客气，有事随时说。")
    _, body, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer, mode="on", humans=1
    )
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "只有一位真人" in turn
    assert stream.final_text == "不客气，有事随时说。"
    assert decision.outcome == "dispatch_reply" and decision.probability is None
    assert list(await db_session.scalars(select(WorkTask))) == []


async def test_groups_with_several_people_still_reject_a_direct_reply(
    db_engine, db_session, monkeypatch
):
    answer = plan(action="reply", text="不客气")
    _, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, answer, mode="on", humans=3
    )
    assert stream.final_text == group_observe.NO_REPLY
    assert decision.outcome == "dispatch_invalid"


async def test_mentions_carrying_a_task_code_continue_that_task(db_session):
    from coreman.runtime.worker.chat import work

    first = await observing_bot(db_session)
    assignee = await another_bot(db_session, first, "data_bot", "数据助手")
    row = WorkTask(
        title="查退货率",
        status="done",
        platform="feishu",
        chat_id=CHAT,
        assignee_bot_id=assignee.id,
    )
    db_session.add(row)
    await db_session.commit()
    expected = dispatch.work_session_key(row.code)
    same = await work.continued(db_session, assignee, CHAT, f"{row.code} 再加个渠道", None)
    assert same == expected
    quoted = await work.continued(db_session, assignee, CHAT, "确认", f"{row.code} 完成：…")
    assert quoted == expected
    assert await work.continued(db_session, first, CHAT, row.code, None) is None  # 不是它的任务
    assert await work.continued(db_session, assignee, "oc_other", row.code, None) is None
