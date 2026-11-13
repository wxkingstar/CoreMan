"""子任务的一轮在 worker 里：文本是交代或追问；不出声；收尾时写下结论；所有者的消息不打断它。"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import delivery, jobs, runtime, settings, transcripts
from coreman.core.assistants.approvals import prompt as approval_prompt
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import (
    AssistantJob,
    Bot,
    InteractionState,
    OutboxItem,
    RelayServer,
    Task,
    User,
    WorkTask,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.assistant_jobs import AssistantSubtaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import BRIEF, reloaded, round_task, spawned
from tests.integration.test_assistant_platform_turn_handler import assistant, streams_of
from tests.integration.test_assistant_platform_turn_yield import fresh, owner_message
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


async def started(session: AsyncSession) -> tuple[Bot, User, AssistantJob, Task]:
    """一位能跑轮次的助理派出的子任务，第一轮已认领。"""
    bot, owner = await assistant(session)
    row = await spawned(session, bot)
    return bot, owner, row, await round_task(session, row)


async def run_as(
    engine: AsyncEngine, session: AsyncSession, task: Task, scenario: str = "normal"
) -> tuple[Task, FakeRelay]:
    relay = FakeRelay(scenario)
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await AssistantSubtaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def rows_in(session: AsyncSession, model: type, bot: Bot) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.bot_id == bot.id)  # type: ignore[attr-defined]
        )
        or 0
    )


async def left_today(session: AsyncSession, bot: Bot) -> int:
    prefs = await settings.get(session, bot.id)
    return await delivery.budget_left(session, bot, settings.local_today(prefs, utcnow()))


async def test_the_round_gets_the_brief_and_stores_the_conclusion(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    before = await left_today(db_session, bot)
    with capture_logs() as logs:
        done, relay = await run_as(db_engine, db_session, task)
    assert done.status == "succeeded"
    body = relay.requests[0]
    text = user_input(body)
    assert text.startswith("这是主会话派给你的子任务") and BRIEF in text
    # 同一条助理记录、同一个沙箱、所有者的个人工具。
    assert body["sandbox"] is True and "COREMAN_FEISHU_PERSONAL_TOKEN" in body["env_vars"]
    system = body["messages"][0]["content"]
    assert "## 你现在是子任务" in system
    assert "\n## 子任务\n" not in system and approval_prompt.RULES not in system
    finished = await reloaded(db_session, row)
    assert finished.status == "done" and finished.started_at is not None
    assert finished.result_enc is not None and "你好" in finished.result_enc
    assert "✅" not in finished.result_enc
    # 对话记录记在子任务自己的会话键下，自然计入当天的轮次预算。
    logged = await raw(
        db_session,
        "SELECT session_key || '|' || message_type FROM chat_logs WHERE task_id = :id",
        id=task.id,
    )
    assert logged == f"{row.session_key}|{SUBTASK_KIND}"
    assert await left_today(db_session, bot) == before - 1
    # 库里与日志里都没有明文。
    stored = str(
        await raw(
            db_session,
            "SELECT t.payload::text || coalesce(t.result::text, '') || j.result_enc"
            " FROM tasks t, assistant_jobs j WHERE t.id = :task AND j.id = :job",
            task=task.id,
            job=row.id,
        )
    )
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    for where in (stored, dumped):
        assert SECRET not in where and "李四" not in where and "你好" not in where


async def test_the_round_says_nothing_in_the_owners_chat(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    await run_as(db_engine, db_session, task)
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["_silent"] is True and stream.is_complete
    assert await rows_in(db_session, OutboxItem, bot) == 0
    # 不出现在公共任务页。
    assert await db_session.scalar(select(func.count()).select_from(WorkTask)) == 0


async def test_a_question_for_the_user_becomes_the_conclusion(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """子任务用了 CLI 的提问工具：不开选择卡（会挂在所有者的私聊上），题面留在结论里。"""
    bot, _, row, task = await started(db_session)
    done, _ = await run_as(db_engine, db_session, task, "ask_user")
    assert done.status == "succeeded"
    assert await rows_in(db_session, InteractionState, bot) == 0
    assert await rows_in(db_session, OutboxItem, bot) == 0
    finished = await reloaded(db_session, row)
    assert finished.status == "done" and finished.result_enc


async def test_a_round_that_reported_being_blocked_ends_blocked(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    await jobs.block(db_session, bot.id, task, "要所有者确认预算")
    await db_session.commit()
    await run_as(db_engine, db_session, task)
    finished = await reloaded(db_session, row)
    assert finished.status == "blocked" and finished.blocked_enc == "要所有者确认预算"
    assert finished.result_enc


async def test_a_failed_round_fails_the_job_with_a_code_only(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    done, _ = await run_as(db_engine, db_session, task, "error_finish")
    assert done.status == "failed"
    finished = await reloaded(db_session, row)
    assert finished.status == "failed" and finished.error_code == done.error_code
    assert finished.result_enc is None
    assert await rows_in(db_session, OutboxItem, bot) == 0


async def test_a_round_whose_job_was_cancelled_never_reaches_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    await jobs.cancel(db_session, bot.id, row.id)
    await db_session.commit()
    done, relay = await run_as(db_engine, db_session, await fresh(db_session, task))
    assert done.status == "cancelled" and relay.requests == []
    assert (await reloaded(db_session, row)).status == "cancelled"


async def test_the_owners_messages_and_the_subtask_leave_each_other_alone(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    # 主会话里所有者的消息正在跑：子任务不用让路。
    message = await owner_message(db_session, bot)
    done, relay = await run_as(db_engine, db_session, task)
    assert done.status == "succeeded" and len(relay.requests) == 1
    assert (await fresh(db_session, message)).cancel_requested_at is None
    # 反过来，所有者的消息开轮时取消的是主会话键上的任务，够不着子任务的。
    second = await spawned(db_session, bot)
    waiting = await round_task(db_session, second)
    await tasks.supersede(db_session, bot.id, "oc_private", except_task_id=message.id)
    await db_session.commit()
    assert (await fresh(db_session, waiting)).cancel_requested_at is None


async def test_a_follow_up_runs_in_the_same_session(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, row, task = await started(db_session)
    _, relay = await run_as(db_engine, db_session, task)
    first = relay.requests[0]
    # 节点把这一轮的转录交回平台之后，下一轮才续得上。
    node = await db_session.get(RelayServer, bot.relay_server_id)
    assert node is not None and node.runtime_node_id is not None
    await transcripts.upload(
        db_session,
        node_id=node.runtime_node_id,
        transfer=uuid.UUID(first["transcript"]["transfer"]),
        status="ok",
        data=b"\x1f\x8bdemo",
    )
    await jobs.message(db_session, bot, row.id, "合同里怎么写的？")
    await db_session.commit()
    again = await round_task(db_session, row)
    assert again.id != task.id and again.session_key == task.session_key
    _, relay = await run_as(db_engine, db_session, again)
    second = relay.requests[0]
    assert second["session_id"] == first["session_id"]
    assert second["transcript"]["restore"] is True
    text = user_input(second)
    assert text.startswith(f"主会话就子任务 {row.id} 追问（第 2 轮") and "合同里怎么写的？" in text
    assert BRIEF not in text
    finished = await reloaded(db_session, row)
    assert (finished.status, finished.rounds) == ("done", 2)
    # 子任务会话是它自己的一行，不是主会话。
    keyed = await raw(
        db_session,
        "SELECT relay_session_id::text FROM chat_sessions"
        " WHERE bot_id = :bot AND session_key = :key",
        bot=bot.id,
        key=row.session_key,
    )
    assert keyed == first["session_id"]


async def test_hitting_the_quota_fails_the_job_without_a_switch_card(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """触限的那一轮：不给所有者发切换卡、不占他私聊里的待答状态，子任务按失败收尾。"""
    bot, _, row, task = await started(db_session)
    done, _ = await run_as(db_engine, db_session, task, "rate_limit")
    assert (done.status, done.error_code) == ("failed", "rate_limited")
    assert await rows_in(db_session, InteractionState, bot) == 0
    assert await rows_in(db_session, OutboxItem, bot) == 0
    finished = await reloaded(db_session, row)
    assert (finished.status, finished.error_code) == ("failed", "rate_limited")
    assert finished.result_enc is None


async def test_a_runtime_that_pauses_after_the_checks_is_not_announced(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开轮前的第二道暂停检查：没有入站事件的一轮不私信所有者，只结掉任务。"""
    bot, _, row, task = await started(db_session)
    answers = iter([None, "runtime_offline"])

    async def blocker(session: AsyncSession, bot: Bot) -> str | None:
        return next(answers)

    monkeypatch.setattr(runtime, "turn_blocker", blocker)
    done, relay = await run_as(db_engine, db_session, task)
    assert (done.status, done.error_code) == ("failed", "assistant_paused")
    assert relay.requests == [] and await rows_in(db_session, OutboxItem, bot) == 0
