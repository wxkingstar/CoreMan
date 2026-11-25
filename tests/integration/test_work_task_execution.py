"""人派给 AI 员工的任务：领取开工后在代表人的私聊里跑一轮，本轮块带细节线，交付后进待验收。"""

from __future__ import annotations

from sqlalchemy import select

from coreman.core.bus import instances, tasks
from coreman.core.db.models import (
    OutboxItem,
    Task,
    User,
    UserIdentity,
    UserReached,
    WorkTask,
    WorkTaskRun,
)
from coreman.core.work_tasks import execute, service
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes import fake_relay
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _run(db_engine, db_session, monkeypatch, answer: str):
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
    await instances.register(
        db_session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    await db_session.commit()
    turn = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    relay = fake_relay.FakeRelay("work_answer")
    ctx = build_ctx(db_engine, turn, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await db_session.get(Task, turn.id, populate_existing=True)
    assert relay.requests, (done.status, done.error_code, done.error_message)
    return done, relay.requests[0]


async def test_claimed_task_runs_in_the_principals_dm_and_goes_to_review(
    db_engine, db_session, monkeypatch
):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice = User(login_name="alice", display_name="alice", email="alice@example.test")
    db_session.add(alice)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    db_session.add(UserReached(bot_id=bot.id, user_id=alice.id, platform_chat_id="oc_dm"))
    row = await service.create(
        db_session,
        alice,
        title="查上周退货率",
        summary="给周会用",
        brief="口径按退货单数除以签收单数，按渠道拆开",
        assignee=service.Assignee("bot", bot.id),
    )
    await service.comment(db_session, row, alice, "渠道 A 的数据去掉测试单")
    await execute.start(db_session, row, bot=bot, trigger="chat", triggered_by={"name": "alice"})
    await db_session.commit()

    done, request = await _run(
        db_engine, db_session, monkeypatch, f"{row.code} 完成：上周退货率 4.2%，渠道 A 最高"
    )
    assert done.status == "succeeded", (done.error_code, done.error_message)
    prompt = str(request)
    assert "口径按退货单数除以签收单数，按渠道拆开" in prompt
    assert "渠道 A 的数据去掉测试单" in prompt
    assert "你和alice的私聊" in prompt and "交给alice验收" in prompt

    row = await db_session.scalar(
        select(WorkTask).where(WorkTask.id == row.id).execution_options(populate_existing=True)
    )
    assert row.status == "review" and row.summary == "上周退货率 4.2%，渠道 A 最高"
    assert row.ask["kind"] == "review" and row.ask["to_user_id"] == str(alice.id)
    run = await db_session.scalar(select(WorkTaskRun).where(WorkTaskRun.work_task_id == row.id))
    assert run.status == "done" and run.trigger == "chat" and run.ended_at is not None
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{row.id}:%"))
    )
    assert trigger.target["chat_id"] == "oc_dm"
