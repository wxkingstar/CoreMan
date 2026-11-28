"""第二期：AI 员工拆子任务、问人、等结果，结果交回上级任务接着做。"""

from __future__ import annotations

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import (
    Bot,
    OutboxItem,
    Task,
    TaskStream,
    User,
    UserIdentity,
    UserReached,
    WorkTask,
)
from coreman.core.work_tasks import agent_tools, execute, flow, service
from tests.integration.test_work_task_execution import _run
from tests.integration.worker_helpers import seed_bot

BASE = "https://coreman.example.com"


async def _setup(db_session):
    lead, _, _ = await seed_bot(db_session)
    lead.platform = "feishu"
    helper = Bot(
        bot_key="data_bot",
        platform="feishu",
        name="数据助手",
        created_by=lead.created_by,
        relay_server_id=lead.relay_server_id,
        model=lead.model,
        working_dir="/data/skills/data_bot",
        system_prompt="你是数据助手",
        merged_system_prompt="你是数据助手",
        credentials_enc=lead.credentials_enc,
        env_vars_enc=lead.env_vars_enc,
    )
    db_session.add(helper)
    alice = User(login_name="alice", display_name="alice", email="alice@example.test")
    db_session.add(alice)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    db_session.add(UserReached(bot_id=lead.id, user_id=alice.id, platform_chat_id="oc_dm"))
    parent = await service.create(
        db_session,
        alice,
        title="出上周经营周报",
        brief="周报要含退货率",
        assignee=service.Assignee("bot", lead.id),
    )
    await execute.start(db_session, parent, bot=lead, trigger="chat", triggered_by={})
    await db_session.commit()
    turn = await db_session.scalar(select(Task).where(Task.session_key == f"work:{parent.code}"))
    return lead, helper, alice, parent, turn


async def _fresh(db_session, row: WorkTask) -> WorkTask:
    return await db_session.scalar(
        select(WorkTask).where(WorkTask.id == row.id).execution_options(populate_existing=True)
    )


async def test_subtask_runs_silently_and_its_result_resumes_the_parent(
    db_engine, db_session, monkeypatch
):
    lead, helper, alice, parent, turn = await _setup(db_session)
    # 上级员工的这一轮已经跑起来了：先让它把开工那一轮跑掉，任务进行中。
    await _run(db_engine, db_session, monkeypatch, "收到，开始做")
    parent = await _fresh(db_session, parent)

    # 人话超长：退回让它改短。
    bad = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "create_task",
        {"title": "x" * 31, "brief": "b", "assignee": {"type": "bot", "id": str(helper.id)}},
        chat_task_id=turn.id,
        base_url=BASE,
    )
    assert bad["stop"] is False and "30" in bad["message"]
    made = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "create_task",
        {
            "title": "查上周退货率",
            "brief": "口径按退货单数除以签收单数，按渠道拆开",
            "assignee": {"type": "bot", "id": str(helper.id)},
        },
        chat_task_id=turn.id,
        base_url=BASE,
    )
    assert "code" in made, made
    # 不能派回给链上已有的员工。
    loop = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "create_task",
        {"title": "再查一次", "brief": "b", "assignee": {"type": "bot", "id": str(lead.id)}},
        chat_task_id=turn.id,
        base_url=BASE,
    )
    assert loop["stop"] is False and "转圈" in loop["message"]
    waited = await agent_tools.invoke(
        db_session, parent, lead, "wait_for", {}, chat_task_id=turn.id, base_url=BASE
    )
    assert waited["ok"] is True
    await db_session.commit()
    parent = await _fresh(db_session, parent)
    assert parent.status == "waiting" and parent.waiting_on["kind"] == "task"

    # 子任务这一轮不出声，交付后结果交回上级。
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == made["code"]))
    assert child.requester_bot_id == lead.id and child.principal_user_id == alice.id
    done, request = await _run(
        db_engine, db_session, monkeypatch, f"{child.code} 完成：上周退货率 4.2%"
    )
    assert done.bot_id == helper.id and "拆给你的" in str(request)
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == done.id))
    assert stream.reply_context.get("_silent") is True
    child = await _fresh(db_session, child)
    assert child.status == "done" and child.summary == "上周退货率 4.2%"
    parent = await _fresh(db_session, parent)
    assert parent.status == "waiting" and parent.waiting_on["kind"] == "queue"
    resumed = await db_session.scalar(
        select(Task)
        .where(Task.session_key == f"work:{parent.code}", Task.status == "queued")
        .order_by(Task.id.desc())
    )
    assert resumed.payload["work_trigger"] == "resume"
    assert "上周退货率 4.2%" in resumed.payload["work_note"]

    # 有子任务的交付必须带完整汇报。
    claimed = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    parent = await _fresh(db_session, parent)
    missing = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "submit_result",
        {"result": "周报已出", "output": "全文", "digest": "记要"},
        chat_task_id=claimed.id,
        base_url=BASE,
    )
    assert missing["stop"] is False and "report" in missing["message"]


async def test_asking_a_person_sends_a_card_and_the_answer_resumes_the_task(db_session):
    lead, _, alice, parent, turn = await _setup(db_session)
    asked = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "ask_human",
        {
            "question": "周报发全员还是只发管理层？",
            "options": ["全员", "管理层"],
            "why": "你是这份周报的负责人",
            "context": "上周有涉及薪资的数据，发全员可能不合适",
        },
        chat_task_id=turn.id,
        base_url=BASE,
    )
    assert "code" in asked, asked
    await agent_tools.invoke(
        db_session, parent, lead, "wait_for", {}, chat_task_id=turn.id, base_url=BASE
    )
    await db_session.commit()
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == asked["code"]))
    assert child.ask["kind"] == "question" and child.ask["to_user_id"] == str(alice.id)
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{child.id}:ask:%"))
    )
    assert card.target == {"user_id": "ou_a"} and "管理层" in str(card.payload["card"])

    # 在卡片或网页上答（「其他」写自己的话）：问题完成，上级任务接着做，答复原文带回去。
    before = child.status
    await execute.answer(
        db_session,
        child,
        alice,
        ask_id=child.ask["id"],
        option="other",
        text="只发管理层，薪资部分去掉",
        channel="feishu",
    )
    await flow.settle(
        db_session, child, before_ask=None, before_status=before, actor_id=alice.id, base_url=BASE
    )
    await db_session.commit()
    child = await _fresh(db_session, child)
    assert child.status == "done"
    resumed = await db_session.scalar(
        select(Task).where(
            Task.session_key == f"work:{parent.code}",
            Task.payload["work_trigger"].astext == "resume",
        )
    )
    assert "只发管理层，薪资部分去掉" in resumed.payload["work_note"]


async def test_a_turn_that_waits_on_subtasks_leaves_the_task_waiting(
    db_engine, db_session, monkeypatch
):
    from coreman.core.db.models import WorkTaskRun

    lead, helper, alice, parent, turn = await _setup(db_session)
    # 派了子任务并 wait_for 之后，有人接着找它（这一轮开始时任务已在等子任务）。
    made = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "create_task",
        {
            "title": "查退货率",
            "brief": "按渠道拆",
            "assignee": {"type": "bot", "id": str(helper.id)},
        },
        chat_task_id=turn.id,
        base_url=BASE,
    )
    await agent_tools.invoke(
        db_session, parent, lead, "wait_for", {}, chat_task_id=turn.id, base_url=BASE
    )
    await db_session.commit()
    await _run(db_engine, db_session, monkeypatch, f"已派给数据助手（{made['code']}），等结果")
    parent = await _fresh(db_session, parent)
    assert parent.status == "waiting" and parent.waiting_on["kind"] == "task"
    run = await db_session.scalar(
        select(WorkTaskRun).where(WorkTaskRun.work_task_id == parent.id).order_by(WorkTaskRun.id)
    )
    assert run.status == "waiting"


async def test_a_turn_ending_after_its_resume_is_queued_does_not_finish_the_task(db_session):
    """wait_for 之后这一轮还没结束，子任务就有了结果、平台已排上接着做的一轮：这一轮不改任务。"""
    from types import SimpleNamespace

    from coreman.runtime.worker.chat import work
    from coreman.runtime.worker.chat.models import Verdict

    lead, _, alice, parent, turn = await _setup(db_session)
    # 这一轮已经开工。
    parent.status, parent.waiting_on = "in_progress", None
    await db_session.flush()
    asked = await agent_tools.invoke(
        db_session,
        parent,
        lead,
        "ask_human",
        {"question": "发全员吗？", "options": ["是", "否"], "context": "周报"},
        chat_task_id=turn.id,
        base_url=BASE,
    )
    await agent_tools.invoke(
        db_session, parent, lead, "wait_for", {}, chat_task_id=turn.id, base_url=BASE
    )
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == asked["code"]))
    await execute.answer(
        db_session, child, alice, ask_id=child.ask["id"], option="o2", text="", channel="web"
    )
    await flow.settle(
        db_session, child, before_ask=None, before_status="todo", actor_id=alice.id, base_url=BASE
    )
    await db_session.commit()
    ctx = SimpleNamespace(task=SimpleNamespace(id=turn.id, payload={}), public_base_url=BASE)
    intake = SimpleNamespace(session_key=f"work:{parent.code}", bot=lead)
    verdict = Verdict("success", "succeeded", None, None, "已问 alice，等她回答")
    await work.final_transition(db_session, ctx, intake, verdict, "")
    await db_session.commit()
    parent = await _fresh(db_session, parent)
    assert parent.status == "waiting" and parent.waiting_on["kind"] == "queue"
    assert parent.closed_at is None


async def test_a_dispatcher_led_task_may_ask_people_more_often(db_session):
    """普通任务一条链最多问人 3 次；调度员牵头的任务分几步推进，放宽到 10 次。"""
    lead, _, _, parent, turn = await _setup(db_session)

    async def ask(n: int):
        return await agent_tools.invoke(
            db_session,
            parent,
            lead,
            "ask_human",
            {"question": f"第 {n} 个口径怎么定？", "context": "背景"},
            chat_task_id=turn.id,
            base_url=BASE,
        )

    for n in range(agent_tools.MAX_ASKS_PER_CHAIN):
        assert "code" in await ask(n)
    refused = await ask(9)
    assert refused["stop"] is True and "3 次" in refused["message"]

    parent.source, parent.dispatcher_bot_id = "dispatch", lead.id
    await db_session.flush()
    assert "code" in await ask(9)
