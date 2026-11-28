"""群聊调度员（第一期试运行）：换判断题、排调度回合、校验决定并记成 shadow 任务，群里不发言。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.chat import decider as decider_mod
from coreman.core.chat import dispatch, group_observe
from coreman.core.db.models import (
    FeishuSentMessage,
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
    db_engine,
    db_session,
    monkeypatch,
    answer: str,
    mode="shadow",
    humans: int | None = None,
    prepare=None,
    as_error: bool = False,
    answer_factory=None,
    text_factory=None,
):
    from coreman.core.bus import instances, tasks
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes import fake_relay

    def content():
        # 要等 prepare 建好的数据（例如任务编号）才写得出的决定，开流时再生成。
        return answer_factory() if answer_factory else answer

    monkeypatch.setitem(
        fake_relay.SCENARIOS,
        "dispatch_plan",
        lambda: [
            # 驱动报错时把错误原文当正文回传，并带上出错标记。
            fake_relay._chunk({"role": "assistant", "content": content()}, error=True)
            if as_error
            else fake_relay._text(content()),
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
    if prepare is not None:
        await prepare(bot)

    async def employees(session, dispatcher, chat_id, requester_id, cipher):
        return staff, []

    async def human_count(bot, chat_id, cipher):
        return humans

    monkeypatch.setattr(dispatch, "employees", employees)
    monkeypatch.setattr(dispatch, "human_count", human_count)
    configured = record_schedule_configure(monkeypatch)
    text = text_factory() if text_factory else "上周退货率多少？谁帮我查一下"
    await say(db_session, bot, "om1", text)
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
    assert task.status == "waiting"
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
    # 任务会话里要别的员工配合走子任务，不挂在群里求助的入口。
    assert "COREMAN_COLLABORATION_URL" not in body.get("env_vars", {})
    assert reply.reply_context["message_id"] == "om1"  # 回复发起人的原消息
    await db_session.refresh(task)
    assert task.status == "done" and task.summary == "上周退货率 4.2%"
    # 跟进的任务：回复末尾写任务的状态，不写「回复已生成」。
    assert reply.final_text.endswith("✅ 已完成") and "回复已生成" not in reply.final_text
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
    assert not task.tracked and task.status == "waiting"  # 没写 track：照样交接、记下
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


@pytest.mark.parametrize(
    ("humans", "quiet_for", "judged"),
    [(3, 5, False), (3, 9, True), (1, 4, True), (1, 2, False)],
)
async def test_dispatcher_waits_less_for_the_group_to_go_quiet(
    db_engine, db_session, monkeypatch, humans, quiet_for, judged
):
    """调度员比旁听默认的 15 秒等得短：多人群 8 秒，单人群 3 秒。"""

    async def human_count(bot, chat_id, cipher):
        return humans

    monkeypatch.setattr(dispatch, "human_count", human_count)
    bot = await dispatcher_bot(db_session)
    await say(db_session, bot, "om1", "上周退货率多少？")
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot, seconds=quiet_for)
    decider = FakeDecider(0.1)
    done = await run_observe(db_engine, db_session, observe, decider)
    decided = await db_session.scalar(select(GroupObserveDecision))
    assert (decided is not None) is judged
    if not judged:
        assert done.run_after > datetime.now(UTC)  # 还没安静够：延后再看


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
        requester_user_id=first.created_by,
    )
    db_session.add(row)
    await db_session.commit()
    expected = dispatch.work_session_key(row.code)
    ask = {"speaker_id": first.created_by}
    same = await work.continued(db_session, assignee, CHAT, f"{row.code} 再加个渠道", None, **ask)
    assert same == expected
    quoted = await work.continued(db_session, assignee, CHAT, "确认", f"{row.code} 完成：…", **ask)
    assert quoted == expected
    assert await work.continued(db_session, first, CHAT, row.code, None, **ask) is None  # 不是它的
    assert await work.continued(db_session, assignee, "oc_other", row.code, None, **ask) is None
    # 别人带编号找它也续进去：人工触发，这一轮起以他的身份做（见 drive_follow_up）。
    assert (
        await work.continued(db_session, assignee, CHAT, row.code, None, speaker_id=uuid.uuid4())
        == expected
    )


async def _untracked_task(db_session, assignee, requester_id, **fields):
    row = WorkTask(
        title="查退货率",
        status="done",
        platform="feishu",
        chat_id=CHAT,
        assignee_bot_id=assignee.id,
        requester_user_id=requester_id,
        tracked=False,
        **fields,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_replying_to_an_answer_without_a_code_continues_that_task(db_session):
    from coreman.runtime.worker.chat import work

    first = await observing_bot(db_session)
    assignee = await another_bot(db_session, first, "data_bot", "数据助手")
    row = await _untracked_task(db_session, assignee, first.created_by)
    db_session.add_all(
        [
            FeishuSentMessage(
                bot_id=assignee.id,
                message_id="om_answer",
                chat_id=CHAT,
                text="上周退货率 4.2%",
                work_code=row.code,
            ),
            FeishuSentMessage(
                bot_id=assignee.id, message_id="om_plain", chat_id=CHAT, text="普通回答"
            ),
        ]
    )
    await db_session.commit()
    expected = dispatch.work_session_key(row.code)
    ask = {"speaker_id": first.created_by}
    assert (
        await work.continued(
            db_session, assignee, CHAT, "那上上周呢", "4.2%", parent_id="om_answer", **ask
        )
        == expected
    )
    other = {"speaker_id": uuid.uuid4()}
    assert (
        await work.continued(
            db_session, assignee, CHAT, "那上上周呢", "4.2%", parent_id="om_answer", **other
        )
        == expected
    )
    # 回复的是别的消息：明确不是在接这件事，也不走两小时的就近续接。
    assert (
        await work.continued(
            db_session,
            assignee,
            CHAT,
            "这个呢",
            "普通回答",
            parent_id="om_plain",
            speaker_id=first.created_by,
        )
        is None
    )


async def test_plain_mention_continues_the_recent_untracked_task_of_the_same_person(db_session):
    from coreman.runtime.worker.chat import work

    first = await observing_bot(db_session)
    assignee = await another_bot(db_session, first, "data_bot", "数据助手")
    row = await _untracked_task(db_session, assignee, first.created_by)
    expected = dispatch.work_session_key(row.code)
    ask = {"speaker_id": first.created_by}
    assert await work.continued(db_session, assignee, CHAT, "那上上周呢", None, **ask) == expected
    assert (
        await work.continued(
            db_session, assignee, CHAT, "那上上周呢", None, speaker_id=uuid.uuid4()
        )
        is None
    )  # 别人问的不算
    assert await work.continued(db_session, first, CHAT, "那上上周呢", None, **ask) is None

    row.updated_at = datetime.now(UTC) - timedelta(minutes=121)
    await db_session.commit()
    assert await work.continued(db_session, assignee, CHAT, "那上上周呢", None, **ask) is None

    # 跟进的任务一样：用户不一定记得带编号。
    row.updated_at, row.tracked = datetime.now(UTC), True
    await db_session.commit()
    assert await work.continued(db_session, assignee, CHAT, "那上上周呢", None, **ask) == expected


async def test_new_task_note_carries_results_of_related_finished_tasks(db_session):
    from types import SimpleNamespace

    from coreman.runtime.worker.chat import work

    first = await observing_bot(db_session)
    assignee = await another_bot(db_session, first, "data_bot", "数据助手")
    done = await _untracked_task(db_session, assignee, first.created_by, summary="一句话")
    db_session.add(
        WorkTaskEvent(
            work_task_id=done.id,
            kind="done",
            actor_type="bot",
            agent_detail={"reply": "上周退货率 4.2%，主要是渠道 A"},
        )
    )
    running = await _untracked_task(db_session, assignee, first.created_by)
    running.status = "in_progress"
    new = await _untracked_task(db_session, assignee, first.created_by, brief="加个渠道维度")
    new.related_codes = [done.code, running.code]
    await db_session.commit()
    handed = SimpleNamespace(task=SimpleNamespace(payload={"work_task_id": new.id}))
    text = await work.note(
        db_session,
        handed,
        SimpleNamespace(session_key=dispatch.work_session_key(new.code), chat_type="group"),
    )
    assert "上周退货率 4.2%，主要是渠道 A" in text
    assert running.code not in text
    assert "接着找你" not in text

    # 不是交接、是发起人接着问的那一轮：提醒员工问的可能是另一件事。
    asked = SimpleNamespace(task=SimpleNamespace(payload={}))
    text = await work.note(
        db_session,
        asked,
        SimpleNamespace(session_key=dispatch.work_session_key(new.code), chat_type="group"),
    )
    assert "接着找你" in text


MENTION_DATA_BOT = [{"key": "@_user_1", "name": "数据助手", "id": {"open_id": "ou_x"}}]


async def test_dispatcher_only_watches_when_someone_mentions_an_employee(db_session):
    bot = await dispatcher_bot(db_session)
    await another_bot(db_session, bot, "data_bot", "数据助手")
    await say(db_session, bot, "om1", "@_user_1 目前用的是网页吗", mentions=MENTION_DATA_BOT)
    assert await tasks_of(db_session, bot, "group_observe") == []
    state = await db_session.get(GroupObserveState, (bot.id, CHAT))
    assert state.pending_since is None and state.bot_streak == 0
    # 消息照样进上下文。
    lines = await group_observe.transcript(db_session, bot, CHAT)
    assert [line.text for line in lines] == ["@数据助手 目前用的是网页吗"]


async def test_mentioning_an_employee_keeps_an_earlier_message_waiting(db_session):
    bot = await dispatcher_bot(db_session)
    await another_bot(db_session, bot, "data_bot", "数据助手")
    await say(db_session, bot, "om1", "谁帮我查一下退货率")
    await say(db_session, bot, "om2", "@_user_1 目前用的是网页吗", mentions=MENTION_DATA_BOT)
    assert len(await tasks_of(db_session, bot, "group_observe")) == 1
    state = await db_session.get(GroupObserveState, (bot.id, CHAT))
    await db_session.refresh(state)
    assert state.pending_since is not None


async def test_mentioning_a_person_or_a_non_dispatcher_still_queues_a_judgment(db_session):
    bot = await dispatcher_bot(db_session)
    await another_bot(db_session, bot, "data_bot", "数据助手")
    person = [{"key": "@_user_1", "name": "同事甲", "id": {"open_id": "ou_y"}}]
    await say(db_session, bot, "om1", "@_user_1 帮忙看下", mentions=person)
    assert len(await tasks_of(db_session, bot, "group_observe")) == 1

    member = await another_bot(db_session, bot, "ops_bot", "运维助手")
    member.group_observe = "on"
    await db_session.commit()
    await say(db_session, member, "om2", "@_user_1 帮忙看下", mentions=MENTION_DATA_BOT)
    assert len(await tasks_of(db_session, member, "group_observe")) == 1


def test_dispatcher_note_says_to_leave_mentioned_employees_alone():
    text = dispatch.note(STAFF, [], [], {}, solo=True)
    assert "@ 了某位 AI 员工" in text and "除了上面 @ 员工的情况，不要选 none" in text


LEAD = {
    "title": "订单与仓储系统对接",
    "summary": "两个系统对齐接口并联调",
    "brief": "先对齐接口，再各自开发，最后在测试环境联调",
}


def test_parse_accepts_and_checks_lead():
    led = dispatch.parse(
        plan(action="lead", lead={**LEAD, "title": "很" * 50, "related": ["T-1", "T-9"]}),
        STAFF,
        {"T-1"},
    )
    assert led.action == "lead"
    (item,) = led.tasks
    assert len(item["title"]) == dispatch.TITLE_LIMIT and item["related"] == ["T-1"]
    assert item["brief"] == LEAD["brief"]
    with pytest.raises(ValueError, match="bad_lead"):
        dispatch.parse(plan(action="lead"), STAFF, set())
    with pytest.raises(ValueError, match="missing_title"):
        dispatch.parse(plan(action="lead", lead={"brief": "对接"}), STAFF, set())
    assert "- lead：" in dispatch.note(STAFF, [], [], {})


async def test_dispatcher_in_trial_records_that_it_would_lead(db_engine, db_session, monkeypatch):
    bot, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, plan(action="lead", reason="要分几步", lead=LEAD)
    )
    assert stream.final_text == group_observe.NO_REPLY and decision.outcome == "dispatch_lead"
    (task,) = list(await db_session.scalars(select(WorkTask)))
    assert task.status == "shadow" and dispatch.is_lead(task)
    assert task.assignee_bot_id == bot.id
    from coreman.core.db.models import Task

    assert list(await db_session.scalars(select(Task).where(Task.status == "queued"))) == []


async def test_live_dispatcher_leads_a_goal_in_its_own_task_session(
    db_engine, db_session, monkeypatch
):
    bot, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine,
        db_session,
        monkeypatch,
        plan(action="lead", reason="要分几步", lead=LEAD),
        mode="on",
    )
    (task,) = list(await db_session.scalars(select(WorkTask)))
    assert task.status == "waiting" and task.tracked and dispatch.is_lead(task)
    assert task.assignee_bot_id == bot.id == task.dispatcher_bot_id and task.root_id == task.id
    link = f"[{task.code}](http://localhost/work-tasks/{task.code})"
    assert stream.final_text == f"这件事我来牵头：订单与仓储系统对接（{link}）"
    assert decision.outcome == "dispatch_lead" and decision.dispatch["codes"] == [task.code]

    # 牵头的那一轮排给调度员自己，在任务会话里做；回复仍回到群里那条消息上。
    done, body, reply = await _run_work_turn(
        db_engine, db_session, monkeypatch, bot.id, f"{link} 卡住了：群里没有能接的员工"
    )
    assert done.session_key == dispatch.work_session_key(task.code)
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "这件事由你牵头" in turn and LEAD["brief"] in turn
    assert "调度员写的要求" not in turn
    assert reply.reply_context["message_id"] == "om1"
    assert reply.final_text.endswith("处理") and "🟡 卡住了，等" in reply.final_text
    assert "回复已生成" not in reply.final_text

    # 调度回合的名录里，它牵头的任务写明是它自己的。
    note = dispatch.note(STAFF, [], [task], {bot.id: bot.name})
    assert f"- {task.code}" in note and "你牵头" in note


async def test_a_note_for_a_task_waiting_on_subtasks_keeps_it_waiting(db_session):
    """补充交给正在等子任务的任务：照常排一轮，任务仍在等，子任务的结果才交得回去。"""
    from coreman.core.db.models import InboundEvent, Task

    bot = await dispatcher_bot(db_session, "on")
    await say(db_session, bot, "om1", "接口文档放哪了？")
    event = await db_session.scalar(select(InboundEvent))
    waiting_on = {"kind": "task", "codes": ["T-9"], "human_text": "等 T-9 的结果"}
    row = WorkTask(
        title="订单与仓储系统对接",
        status="waiting",
        waiting_on=waiting_on,
        platform="feishu",
        chat_id=CHAT,
        assignee_bot_id=bot.id,
        dispatcher_bot_id=bot.id,
        requester_user_id=bot.created_by,
    )
    db_session.add(row)
    await db_session.flush()
    await dispatch.hand_off(db_session, row, event_id=event.id, requester_pid="u1", note="放在仓库")
    await db_session.commit()
    assert row.status == "waiting" and row.waiting_on == waiting_on
    queued = await db_session.scalar(
        select(Task).where(Task.session_key == dispatch.work_session_key(row.code))
    )
    assert queued.payload["work_note"] == "放在仓库"


# ---------------------------------------------------------------- 运行时额度用完

LIMIT_NOTICE = "You've hit your weekly limit · resets Nov 30 at 4am (America/Los_Angeles)"


@pytest.mark.parametrize("as_error", [False, True], ids=["as_text", "as_error"])
async def test_live_dispatcher_says_so_when_its_runtime_is_out_of_quota(
    db_engine, db_session, monkeypatch, as_error
):
    from coreman.core.i18n.messages import msg

    _, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, LIMIT_NOTICE, mode="on", as_error=as_error
    )
    said = msg("dispatch_rate_limited", "zh")
    assert stream.final_text == said and "额度" in said
    assert decision.outcome == "dispatch_failed"
    assert decision.dispatch["error"] == "rate_limited" and decision.dispatch["said"] == said
    assert list(await db_session.scalars(select(WorkTask))) == []


async def test_shadow_dispatcher_stays_silent_when_out_of_quota(db_engine, db_session, monkeypatch):
    _, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, LIMIT_NOTICE
    )
    assert stream.final_text == group_observe.NO_REPLY
    assert decision.outcome == "dispatch_failed" and decision.dispatch["error"] == "rate_limited"


async def test_the_quota_notice_is_not_repeated_within_the_hour(db_engine, db_session, monkeypatch):
    async def said_already(bot):
        db_session.add(
            GroupObserveDecision(
                bot_id=bot.id,
                chat_id=CHAT,
                mode="on",
                outcome="dispatch_failed",
                dispatch={"error": "rate_limited", "said": "说过了"},
            )
        )
        await db_session.commit()

    _, _, stream, _, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, LIMIT_NOTICE, mode="on", prepare=said_already
    )
    assert stream.final_text == group_observe.NO_REPLY
    latest = await db_session.scalar(
        select(GroupObserveDecision).order_by(GroupObserveDecision.id.desc()).limit(1)
    )
    await db_session.refresh(latest)
    assert latest.dispatch["error"] == "rate_limited" and "said" not in latest.dispatch


async def test_mentioning_a_live_dispatcher_goes_to_a_dispatch_turn(
    db_engine, db_session, monkeypatch
):
    """人 @ 开启中的调度员交代事情：不走普通对话，攒进这一批、不问判断模型，直接排调度回合。"""

    async def human_count(bot, chat_id, cipher):
        return 5

    monkeypatch.setattr(dispatch, "human_count", human_count)
    bot = await dispatcher_bot(db_session, "on")
    turn = await say(
        db_session, bot, "om1", "@调度员 把订单和仓储的对接跟进起来", mentions_bot=True
    )
    assert turn is None and await tasks_of(db_session, bot, "chat") == []
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    assert observe.payload["mentioned"] is True
    await quiet_down(db_session, bot)
    decider = FakeDecider(0.0)
    await run_observe(db_engine, db_session, observe, decider)
    assert decider.instructions == []
    (chat,) = await tasks_of(db_session, bot, "chat")
    assert chat.payload["origin"] == "dispatch" and chat.payload["mentioned"] is True
    assert "solo" not in chat.payload and chat.session_key == dispatch.session_key(CHAT)
    decision = await db_session.scalar(select(GroupObserveDecision))
    assert decision.outcome == "mentioned" and decision.task_id == chat.id

    note = dispatch.note(STAFF, [], [], {}, mentioned=True)
    assert "有人 @ 了你" in note and "- reply：" in note and "- none" not in note


@pytest.mark.parametrize(("mode", "text"), [("shadow", "@调度员 跟进一下对接"), ("on", "/new")])
async def test_trial_dispatchers_and_commands_keep_the_normal_turn(db_session, mode, text):
    """试运行的调度员不在群里说话，@ 它照常对话；命令也不交给调度。"""
    bot = await dispatcher_bot(db_session, mode)
    turn = await say(db_session, bot, "om1", text, mentions_bot=True)
    assert turn is not None and turn.payload.get("origin") is None
    assert await tasks_of(db_session, bot, "group_observe") == []


async def _led_goal(session, bot, **fields):
    """调度员牵头的一件事，代表人是在群里以 `u1` 说话的那位。"""
    from coreman.core.db.models import User, UserIdentity

    user = User(login_name="alice", display_name="alice", email="alice@example.test")
    session.add(user)
    await session.flush()
    session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id="u1"))
    row = WorkTask(
        title="订单与仓储系统对接",
        source="dispatch",
        platform="feishu",
        chat_id=CHAT,
        assignee_bot_id=bot.id,
        dispatcher_bot_id=bot.id,
        requester_user_id=user.id,
        principal_user_id=user.id,
        tracked=True,
        **fields,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.parametrize(
    ("speaker", "outcome"),
    [
        # 这件事等的人回话：不问判断模型，直接排调度回合。
        ("u1", "awaited"),
        # 别人说话：照常由判断模型筛。
        ("u2", "below_threshold"),
    ],
)
async def test_a_reply_from_the_person_a_led_goal_waits_for_reaches_the_dispatcher(
    db_engine, db_session, monkeypatch, speaker, outcome
):
    """牵头的事在等某个人表态时，他在群里回一句「同意」：判断模型看它不像在请人办事，会漏掉。"""

    async def human_count(bot, chat_id, cipher):
        return 5

    monkeypatch.setattr(dispatch, "human_count", human_count)
    bot = await dispatcher_bot(db_session, "on")
    goal = await _led_goal(
        db_session,
        bot,
        status="waiting",
        waiting_on={"kind": "human", "human_text": "两个改动等你同意合并"},
    )
    await say(db_session, bot, "om1", "同意合并", user=speaker)
    (observe,) = await tasks_of(db_session, bot, "group_observe")
    await quiet_down(db_session, bot)
    decider = FakeDecider(0.0)
    await run_observe(db_engine, db_session, observe, decider)
    decision = await db_session.scalar(select(GroupObserveDecision))
    assert decision.outcome == outcome
    chats = await tasks_of(db_session, bot, "chat")
    if outcome == "awaited":
        assert decider.instructions == []
        assert chats[0].payload["awaited"] == [goal.code] and "mentioned" not in chats[0].payload
    else:
        assert len(decider.instructions) == 1 and chats == []


async def test_a_finished_goal_keeps_listening_only_while_it_left_things_for_people(
    db_engine, db_session, monkeypatch
):
    """办完的事：留了要人处理的事，代表人一天内的回话还算在交代它；什么都没留就不再等谁。"""
    from coreman.core.work_tasks import progress

    bot = await dispatcher_bot(db_session, "on")
    goal = await _led_goal(db_session, bot, status="done", closed_at=datetime.now(UTC))
    assert await progress.awaited_users(db_session, goal) == set()
    db_session.add(
        WorkTaskEvent(
            work_task_id=goal.id,
            kind="submitted",
            actor_type="bot",
            agent_detail={"followups": ["alice：同意后合并两个改动"]},
        )
    )
    await db_session.commit()
    assert await progress.awaited_users(db_session, goal) == {goal.principal_user_id}

    note = dispatch.note(STAFF, [], [goal], {}, awaited=[goal.code])
    assert f"你牵头的 {goal.code} 正在等的人在说话" in note and "- none" in note


async def _person(session, pid, name):
    from coreman.core.db.models import User, UserIdentity

    person = await session.scalar(
        select(User).join(UserIdentity).where(UserIdentity.platform_user_id == pid)
    )
    if person is None:
        person = User(login_name=f"p_{pid}", display_name=name)
        session.add(person)
        await session.flush()
        session.add(UserIdentity(user_id=person.id, platform="feishu", platform_user_id=pid))
        await session.flush()
    return person


async def _web_task(session, assignee_id, *, requester="u1", principal="u1", **fields):
    """任务页上建、指派给某位员工、还没开工的任务（没有群归属）。"""
    person = await _person(session, requester, "张三")
    principal_user = await _person(session, principal, "王五") if principal != requester else person
    values = {
        "title": "已卖出的货又变回待收",
        "summary": "先确认约定，再定在哪边修",
        "brief": "现象和复现步骤见附件",
        "status": "todo",
        "source": "web",
        "requester_user_id": person.id,
        "requester_name": person.display_name,
        "principal_user_id": principal_user.id,
        "assignee_bot_id": assignee_id,
    }
    row = WorkTask(**{**values, **fields})
    session.add(row)
    await session.flush()
    row.root_id = row.id
    await session.commit()
    return row


async def test_dispatch_note_shows_mentioned_tasks_from_elsewhere(db_session):
    bot = await dispatcher_bot(db_session)
    mine = await _web_task(db_session, bot.id)
    # 别人的、在别的群、也没指派给调度员的任务：看不到内容。
    helper = await another_bot(db_session, bot, "data_bot", "数据助手")
    other = await _web_task(db_session, helper.id, requester="u9", principal="u9", chat_id="oc_x")
    speaker = mine.principal_user_id
    seen, hidden = await dispatch.referenced_tasks(
        db_session,
        dispatcher=bot,
        chat_id=CHAT,
        speaker_id=speaker,
        texts=[f"看一下{mine.code}", f"还有 {other.code} 和 T-999999"],
        exclude=set(),
    )
    # 说话人能管的带内容，别人的只说看不到，不存在的不提。
    assert [r.code for r in seen] == [mine.code] and hidden == [other.code]
    note = dispatch.note(
        STAFF, [], [], {}, mentioned=True, referenced=seen, hidden=hidden, me=bot.id, chat_id=CHAT
    )
    assert f"- {mine.code}（指派给你，todo，还没开工，不属于任何群）" in note
    assert f"{other.code}：它不在这个群、没有指派给你" in note and "adopt" in note

    # 只说「转给你了」：指派给它、还没开工、代表人是说话人的也带上；本群列表里有的不重复。
    seen, _ = await dispatch.referenced_tasks(
        db_session,
        dispatcher=bot,
        chat_id=CHAT,
        speaker_id=speaker,
        texts=["转给你了"],
        exclude=set(),
    )
    assert [r.code for r in seen] == [mine.code]
    seen, _ = await dispatch.referenced_tasks(
        db_session,
        dispatcher=bot,
        chat_id=CHAT,
        speaker_id=speaker,
        texts=["转给你了"],
        exclude={mine.code},
    )
    assert seen == []


def test_parse_adopt_only_takes_mentioned_tasks():
    answer = plan(action="adopt", task_code="T-7", note="先复现")
    assert dispatch.parse(answer, STAFF, set(), referenced={"T-7"}).task_code == "T-7"
    with pytest.raises(ValueError, match="bad_adopt"):
        dispatch.parse(answer, STAFF, {"T-7"})


async def test_live_dispatcher_adopts_a_web_task_assigned_to_it_and_leads(
    db_engine, db_session, monkeypatch
):
    holder = {}

    async def prepare(bot):
        holder["task"] = await _web_task(db_session, bot.id)

    bot, request, stream, decision, _, _ = await _dispatch_turn(
        db_engine,
        db_session,
        monkeypatch,
        "",
        mode="on",
        prepare=prepare,
        # 不带编号：只说「转给你了」，靠指派给它、还没开工、代表人是说话人认出来。
        text_factory=lambda: "转给你了",
        answer_factory=lambda: plan(
            action="adopt",
            reason="指派给我的",
            task_code=holder["task"].code,
            note="先在测试环境复现",
        ),
    )
    task = await db_session.get(WorkTask, holder["task"].id, populate_existing=True)
    turn_text = json.dumps(request["messages"][-1]["content"], ensure_ascii=False)
    assert f"{task.code}（指派给你" in turn_text
    link = f"[{task.code}](http://localhost/work-tasks/{task.code})"
    assert stream.final_text == f"这件事我来牵头：已卖出的货又变回待收（{link}）"
    assert decision.outcome == "dispatch_adopt" and decision.dispatch["codes"] == [task.code]
    assert task.chat_id == CHAT and task.tracked and task.source == "web"
    assert task.status == "waiting" and task.claimed_at is not None and dispatch.is_lead(task)
    assert task.principal_user_id == task.requester_user_id

    # 牵头的那一轮排给调度员自己，带着人写的要求和牵头规则，回复挂在群里那条消息上。
    done, body, reply = await _run_work_turn(
        db_engine, db_session, monkeypatch, bot.id, f"{link} 卡住了：群里没有能接的员工"
    )
    assert done.session_key == dispatch.work_session_key(task.code)
    turn = body["messages"][-1]["content"]
    turn = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
    assert "在群里叫你开工，由你牵头" in turn and "现象和复现步骤见附件" in turn
    assert "这件事由你牵头" in turn and "先在测试环境复现" in turn
    assert reply.reply_context["message_id"] == "om1"


async def _staff_id(session, key):
    from coreman.core.db.models import Bot

    return await session.scalar(select(Bot.id).where(Bot.bot_key == key))


async def _adopt_turn(db_engine, db_session, monkeypatch, make_task):
    holder = {}

    async def prepare(bot):
        holder["task"] = await make_task(bot)

    bot, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine,
        db_session,
        monkeypatch,
        "",
        mode="on",
        prepare=prepare,
        text_factory=lambda: f"做一下 {holder['task'].code}",
        answer_factory=lambda: plan(action="adopt", task_code=holder["task"].code),
    )
    task = await db_session.get(WorkTask, holder["task"].id, populate_existing=True)
    return bot, task, stream, decision


async def test_anyone_can_start_a_task_assigned_to_the_dispatcher_and_drives_it(
    db_engine, db_session, monkeypatch
):
    async def make_task(bot):
        await _person(db_session, "u1", "张三")  # 说话人是平台成员
        return await _web_task(db_session, bot.id, requester="u9", principal="u9")

    bot, task, stream, _ = await _adopt_turn(db_engine, db_session, monkeypatch, make_task)
    assert stream.final_text.startswith("这件事我来牵头") and dispatch.is_lead(task)
    # 以叫开工的人（u1）的身份做：他成为代表人，创建人不变。
    speaker = await _person(db_session, "u1", "张三")
    assert task.principal_user_id == speaker.id != task.requester_user_id


async def test_the_creator_can_start_a_task_assigned_to_another_employee(
    db_engine, db_session, monkeypatch
):
    async def make_task(bot):
        return await _web_task(db_session, await _staff_id(db_session, "data_bot"))

    _, task, stream, _ = await _adopt_turn(db_engine, db_session, monkeypatch, make_task)
    link = f"[{task.code}](http://localhost/work-tasks/{task.code})"
    assert stream.final_text == f"@数据助手 开工了：已卖出的货又变回待收（{link}）"
    assert task.status == "waiting" and task.chat_id == CHAT and not dispatch.is_lead(task)
    from coreman.core.db.models import Task

    queued = await db_session.scalar(
        select(Task).where(Task.session_key == dispatch.work_session_key(task.code))
    )
    assert queued.bot_id == task.assignee_bot_id


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"status": "waiting"}, "已经开工或已结束"),
        ({"chat_id": "oc_other"}, "是别的群里的事"),
        # 不是创建人，任务也没指派给调度员：只能直接找被指派的员工。
        ({"requester": "u9", "to": "data_bot"}, "没有指派给"),
    ],
    ids=["started", "other_group", "not_creator"],
)
async def test_live_dispatcher_says_why_it_cannot_adopt(
    db_engine, db_session, monkeypatch, fields, said
):
    holder = {}

    async def prepare(bot):
        extra = dict(fields)
        to = extra.pop("to", None)
        assignee = await _staff_id(db_session, to) if to else bot.id
        holder["task"] = await _web_task(db_session, assignee, **extra)

    _, _, stream, decision, _, _ = await _dispatch_turn(
        db_engine,
        db_session,
        monkeypatch,
        "",
        mode="on",
        prepare=prepare,
        text_factory=lambda: f"做一下 {holder['task'].code}",
        answer_factory=lambda: plan(action="adopt", task_code=holder["task"].code),
    )
    task = await db_session.get(WorkTask, holder["task"].id, populate_existing=True)
    assert stream.final_text.startswith(task.code) and said in stream.final_text
    assert decision.outcome == "dispatch_adopt"
    assert task.claimed_at is None and task.dispatcher_bot_id is None


async def test_someone_else_adding_to_a_task_in_the_group_drives_it_from_then_on(db_session):
    bot = await dispatcher_bot(db_session, mode="on")
    helper = await another_bot(db_session, bot, "data_bot", "数据助手")
    owner = await _person(db_session, "u1", "张三")
    other = await _person(db_session, "u2", "李四")
    row = WorkTask(
        title="查退货率",
        status="in_progress",
        platform="feishu",
        chat_id=CHAT,
        assignee_bot_id=helper.id,
        requester_user_id=owner.id,
        principal_user_id=owner.id,
        dispatcher_bot_id=bot.id,
    )
    db_session.add(row)
    await db_session.flush()
    await say(db_session, bot, "om9", "顺便看下上上周", user="u2")
    from coreman.core.db.models import InboundEvent

    inbound = await db_session.scalar(
        select(InboundEvent).where(InboundEvent.platform_msg_id == "om9")
    )
    staff = [dispatch.Employee("data_bot", "数据助手", "数据", helper.id)]
    _, line = await dispatch.record(
        db_session,
        plan=dispatch.Plan(action="append", task_code=row.code, note="顺便看下上上周"),
        dispatcher=bot,
        chat_id=CHAT,
        event_id=inbound.id,
        requester_id=other.id,
        requester_name="李四",
        staff=staff,
        decision_id=None,
        chat_task_id=1,
        live=True,
        requester_pid="u2",
    )
    await db_session.commit()
    await db_session.refresh(row)
    # 补充也是人工触发：之后以补充的人的身份做，时间线上记一笔。
    assert line.startswith("补充给 @数据助手") and row.principal_user_id == other.id
    texts = list(
        await db_session.scalars(
            select(WorkTaskEvent.human_text).where(WorkTaskEvent.work_task_id == row.id)
        )
    )
    assert "李四补充，之后以李四的身份做" in texts
