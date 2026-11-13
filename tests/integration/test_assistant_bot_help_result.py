"""目标员工那一轮结束之后：答复加密存进助理的行并投回主会话；要人确认时是 blocked，不发卡片。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import dispatch, jobs
from coreman.core.assistants.kinds import JOB_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import Task
from coreman.runtime.worker.assistant_jobs import AssistantJobResultHandler
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.dispatch_helpers import (
    ANSWER,
    QUESTION,
    asked,
    help_task,
    reply,
    rows_in,
    run_help,
    scripted,
    target_bot,
)
from tests.integration.job_helpers import reloaded, take, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant, run
from tests.integration.worker_helpers import user_input

pytestmark = pytest.mark.usefixtures("assistant_master")
SEALED = (
    "SELECT coalesce(result_enc, '') || coalesce(blocked_enc, '') FROM assistant_jobs"
    " WHERE id = :id"
)
LOGGED = "SELECT status || '|' || coalesce(response_content, '') FROM chat_logs WHERE task_id = :id"


async def test_the_answer_is_sealed_and_returns_to_the_main_session(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session, name="库存助手")
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    done, _ = await run_help(
        db_engine, db_session, task, scripted(monkeypatch, reply("completed", ANSWER))
    )
    assert done.status == "succeeded"
    found = await reloaded(db_session, row)
    assert (found.status, found.result_enc, found.blocked_enc) == ("done", ANSWER, None)
    # 助理这边是密文；目标员工名下的这一轮按裁定 17 照普通私聊的方式留着（可见性见 Task 8）。
    stored = str(await raw(db_session, SEALED, id=row.id))
    assert SECRET not in stored and stored.startswith("enc:v2:d1:")
    assert ANSWER in str(await raw(db_session, LOGGED, id=task.id))
    # 结论作为一轮投回助理的主会话，任务载荷里只有编号。
    (result,) = await tasks_of(db_session, bot, JOB_RESULT_KIND)
    assert result.session_key == "oc_private" and found.result_task_id == result.id
    held = await raw(
        db_session,
        "SELECT strpos(payload::text, :needle) FROM tasks WHERE id = :id",
        needle="周转",
        id=result.id,
    )
    assert held == 0
    turn, relay = await run(
        db_engine, db_session, await take(db_session, result.id), AssistantJobResultHandler()
    )
    assert turn.status == "succeeded"
    text = user_input(relay.requests[0])
    assert dispatch.RESULT_HEADER in text and dispatch.RESULT_FOOTER in text
    assert f"编号：{row.id}" in text and "状态：已答复" in text and "> 库存助手" in text
    # 问题与答复都在引用行里：伪造不出平台写的行。
    assert f"> {QUESTION}" in text and f"> {ANSWER}" in text
    assert (await reloaded(db_session, row)).reported_at is not None


async def test_a_blocked_answer_records_what_is_needed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    answer = reply("blocked", "查到一半", ["需要所有者授权财务系统", "缺 DEMO_API_KEY"])
    done, _ = await run_help(
        db_engine, db_session, await help_task(db_session, row), scripted(monkeypatch, answer)
    )
    assert done.status == "succeeded"
    found = await reloaded(db_session, row)
    assert (found.status, found.result_enc) == ("blocked", "查到一半")
    assert found.blocked_enc == "需要所有者授权财务系统；缺 DEMO_API_KEY"
    assert "授权" not in str(await raw(db_session, SEALED, id=row.id))
    text = await dispatch.result_text(db_session, found)
    assert "状态：对方做不下去，需要所有者处理" in text
    assert "对方需要：\n> 需要所有者授权财务系统；缺 DEMO_API_KEY" in text
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1


async def test_asking_the_user_becomes_blocked_without_a_card(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    # 假 relay 的 ask_user 脚本：先说一句，再用提问工具问「用哪个？」。
    done, _ = await run_help(db_engine, db_session, task, "ask_user")
    assert done.status == "succeeded"
    # 普通轮次这时会写一条待答状态并发选择卡：这里两样都没有。
    assert await rows_in(db_session, "interaction_states") == 0
    assert await rows_in(db_session, "outbox") == 0
    found = await reloaded(db_session, row)
    assert (found.status, found.result_enc, found.blocked_enc) == (
        "blocked",
        "先说一句",
        "用哪个？",
    )
    assert str(await raw(db_session, LOGGED, id=task.id)).startswith("success|")


async def test_prose_is_kept_and_silence_is_a_failure(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    spoken = await asked(db_session, bot, target)
    await run_help(db_engine, db_session, await help_task(db_session, spoken), "normal")
    kept = await reloaded(db_session, spoken)
    # 没按 JSON 作答：终稿原文当作答复交回，由助理自己读。
    assert kept.status == "done" and "你好" in (kept.result_enc or "")
    assert "世界" in (kept.result_enc or "")
    quiet = await asked(db_session, bot, target, question="再查一件")
    done, _ = await run_help(
        db_engine, db_session, await help_task(db_session, quiet), "tools_only"
    )
    assert done.status == "succeeded"
    empty = await reloaded(db_session, quiet)
    assert (empty.status, empty.error_code, empty.result_enc) == ("failed", dispatch.EMPTY, None)
    assert "失败（empty_answer）" in await dispatch.result_text(db_session, empty)


async def test_a_failed_round_is_reported_as_failed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    done, _ = await run_help(
        db_engine, db_session, await help_task(db_session, row), "error_finish"
    )
    assert done.status == "failed"
    found = await reloaded(db_session, row)
    # 只留任务的错误代码；任务没给代码时记 `turn_failed`。
    assert found.status == "failed" and found.error_code in (done.error_code, "turn_failed")
    assert found.result_enc is None and found.finished_at is not None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1
    assert await rows_in(db_session, "outbox") == 0


async def test_cancelling_stops_the_target_round_and_nothing_comes_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    await jobs.cancel(db_session, bot.id, row.id, code="cancelled_by_assistant")
    await db_session.commit()
    flagged = await db_session.get(Task, task.id, populate_existing=True)
    assert flagged is not None and flagged.cancel_requested_at is not None
    done, relay = await run_help(db_engine, db_session, flagged)
    assert done.status == "cancelled" and relay.requests == []
    found = await reloaded(db_session, row)
    assert (found.status, found.error_code) == ("cancelled", "cancelled_by_assistant")
    assert await tasks_of(db_session, bot, JOB_RESULT_KIND) == []


async def test_a_dispatch_cannot_be_followed_up(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    await run_help(
        db_engine,
        db_session,
        await help_task(db_session, row),
        scripted(monkeypatch, reply("completed", ANSWER)),
    )
    with pytest.raises(jobs.JobError) as refused:
        await jobs.message(db_session, bot, row.id, "再问一句")
    assert refused.value.code == "job_no_follow_up"


async def test_a_round_that_never_reached_its_ending_is_closed_by_reconcile(
    db_session: AsyncSession,
) -> None:
    """目标员工那一轮没走到收尾（开流前被取消、worker 失联）：对账按任务号收口，不分任务归谁。"""
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    await tasks.finish(db_session, task.id, status="failed", error_code="worker_lost")
    await db_session.commit()
    now = datetime.now(UTC)
    assert (await jobs.reconcile(db_session, now))["assistant_jobs_lost"] == 0
    counts = await jobs.reconcile(db_session, now + jobs.LOST_GRACE + timedelta(seconds=5))
    await db_session.commit()
    assert counts["assistant_jobs_lost"] == 1
    found = await reloaded(db_session, row)
    assert (found.status, found.error_code) == ("failed", "worker_lost")
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1
    # 到点还没答完的同样收口，并叫停对方那一轮。
    slow = await asked(db_session, bot, target, question="再查一件")
    running = await help_task(db_session, slow)
    late = datetime.now(UTC) + jobs.LIFETIME + timedelta(seconds=5)
    assert (await jobs.reconcile(db_session, late))["assistant_jobs_timed_out"] == 1
    await db_session.commit()
    timed_out = await reloaded(db_session, slow)
    assert (timed_out.status, timed_out.error_code) == ("failed", jobs.TIMEOUT)
    stopped = await db_session.get(Task, running.id, populate_existing=True)
    assert stopped is not None and stopped.cancel_requested_at is not None
    assert "对方到点还没答完" in await dispatch.result_text(db_session, timed_out)
