"""结论投回主会话：载荷只有编号；文本是引用；正常结束才算投到；被打断的换一个键重投，只投到一次。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import jobs
from coreman.core.assistants.kinds import JOB_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantJob, Bot, RelayServer, Task
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.assistant_jobs import AssistantJobResultHandler
from coreman.runtime.worker.chat.models import Verdict
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import reloaded, spawned, take, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant, run, streams_of
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")
RESULT = f"李四上周报价 3 万。{SECRET}"


async def finished(
    session: AsyncSession, *, ok: bool = True, code: str | None = None, **kw: Any
) -> tuple[Bot, AssistantJob]:
    """一位能跑轮次的助理的一个子任务，那一轮刚结束、结论已写下。"""
    bot, _ = await assistant(session)
    row = await spawned(session, bot, **kw)
    assert row.last_task_id is not None
    await tasks.finish(session, row.last_task_id, status="succeeded" if ok else "failed")
    done = await jobs.complete(
        session, bot, row.id, task_id=row.last_task_id, ok=ok, result=RESULT, error_code=code
    )
    await session.commit()
    assert done is not None
    return bot, done


async def test_completing_queues_one_result_turn_that_carries_only_the_id(
    db_session: AsyncSession,
) -> None:
    bot, row = await finished(db_session)
    (task,) = await tasks_of(db_session, bot, JOB_RESULT_KIND)
    assert row.result_task_id == task.id and row.report_seq == 1 and row.reported_at is None
    assert task.session_key == "oc_private" == row.origin_session_key
    assert task.dedupe_key == f"assistant-job:{row.id}:result:1"
    payload = str(
        await raw(db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id)
    )
    assert SECRET not in payload and "报价" not in payload
    opened = await db_session.get(Task, task.id, populate_existing=True)
    assert opened is not None
    assert opened.payload["job_id"] == str(row.id)
    assert not {"title", "brief", "result", "text"} & set(opened.payload)
    # 在途时再问一次：不会多排一轮。
    assert await jobs.report(db_session, bot, row, utcnow()) is None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1


async def test_the_result_turn_quotes_the_conclusion_in_the_main_session(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, row = await finished(db_session)
    turn = await take(db_session, row.result_task_id)
    with capture_logs() as logs:
        done, relay = await run(db_engine, db_session, turn, AssistantJobResultHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert text.startswith(jobs.RESULT_HEADER) and f"编号：{row.id}" in text
    assert f"> {RESULT}" in text
    assert "\n## 子任务\n" in relay.requests[0]["messages"][0]["content"]
    # 主会话里的一轮：回复照常发到所有者的私聊。
    logged = await raw(
        db_session,
        "SELECT session_key || '|' || message_type FROM chat_logs WHERE task_id = :id",
        id=turn.id,
    )
    assert logged == f"oc_private|{JOB_RESULT_KIND}"
    (stream,) = await streams_of(db_session, turn)
    assert stream.reply_context["chat_id"] == "oc_private" and "_silent" not in stream.reply_context
    delivered = await reloaded(db_session, row)
    assert delivered.reported_at is not None and delivered.result_task_id == turn.id
    # 投到了就不再投。
    later = utcnow() + timedelta(hours=1)
    assert await jobs.report(db_session, bot, delivered, later) is None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1
    stored = str(
        await raw(
            db_session,
            "SELECT payload::text || coalesce(result::text, '') FROM tasks WHERE id = :id",
            id=turn.id,
        )
    )
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    for where in (stored, dumped):
        assert SECRET not in where and "报价" not in where


async def test_a_failed_job_is_reported_too_and_a_silent_one_never(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, row = await finished(db_session, ok=False, code="RelayError")
    turn = await take(db_session, row.result_task_id)
    _, relay = await run(db_engine, db_session, turn, AssistantJobResultHandler())
    text = user_input(relay.requests[0])
    assert "状态：失败（RelayError）" in text and "没有结论。" in text
    # 平台派的、不要投回的子任务：做完就完了。
    quiet = await spawned(db_session, bot, origin="system", report=False)
    assert quiet.last_task_id is not None
    done = await jobs.complete(
        db_session, bot, quiet.id, task_id=quiet.last_task_id, ok=True, result="整理好了"
    )
    await db_session.commit()
    assert done is not None and done.status == "done" and done.result_task_id is None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1


@pytest.mark.parametrize(
    ("status", "code", "reported", "failures", "wait"),
    [
        ("succeeded", None, True, 0, None),
        ("cancelled", "superseded", False, 0, 60),
        ("failed", "platform_turn_yielded", False, 0, 60),
        ("failed", "assistant_paused", False, 0, 60),
        ("failed", "RelayError", False, 1, 300),
    ],
)
async def test_the_closing_hook_settles_by_how_the_turn_ended(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    status: str,
    code: str | None,
    reported: bool,
    failures: int,
    wait: int | None,
) -> None:
    _, row = await finished(db_session)
    turn = await take(db_session, row.result_task_id)
    await tasks.finish(db_session, turn.id, status=status, error_code=code)
    await db_session.commit()
    verdict = Verdict("success" if reported else "stopped", status, code, None, "")
    await AssistantJobResultHandler()._after_finalize(
        build_ctx(db_engine, turn), cast(Any, None), verdict
    )
    settled = await reloaded(db_session, row)
    assert (settled.reported_at is not None) == reported
    assert settled.result_task_id == (turn.id if reported else None)
    assert settled.report_failures == failures
    if wait is not None:
        assert settled.report_after is not None
        ahead = settled.report_after - utcnow()
        assert timedelta(seconds=wait - 10) < ahead <= timedelta(seconds=wait)


async def test_a_released_result_goes_again_under_a_new_key_and_arrives_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, row = await finished(db_session)
    first = row.result_task_id
    assert first is not None
    await tasks.finish(db_session, first, status="cancelled", error_code="superseded")
    assert await jobs.settle_report(db_session, first, "cancelled", "superseded") is True
    await db_session.commit()
    now = utcnow()
    waiting = await reloaded(db_session, row)
    # 还没到重投的时刻。
    assert await jobs.report(db_session, bot, waiting, now) is None
    again = await jobs.report(db_session, bot, waiting, now + timedelta(seconds=61))
    await db_session.commit()
    assert again is not None and again.id != first
    assert again.dedupe_key == f"assistant-job:{row.id}:result:2"
    # 在途时不再排；记过账的那一轮再记一次没有效果。
    assert await jobs.report(db_session, bot, waiting, now + timedelta(minutes=10)) is None
    assert await jobs.settle_report(db_session, first, "cancelled", "superseded") is False
    turn = await take(db_session, again.id)
    done, relay = await run(db_engine, db_session, turn, AssistantJobResultHandler())
    assert done.status == "succeeded" and len(relay.requests) == 1
    assert (await reloaded(db_session, row)).reported_at is not None
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 2


async def test_three_real_failures_stop_the_reporting(db_session: AsyncSession) -> None:
    bot, row = await finished(db_session)
    for attempt in range(jobs.MAX_REPORT_FAILURES):
        current = await reloaded(db_session, row)
        task_id = current.result_task_id
        assert task_id is not None
        await tasks.finish(db_session, task_id, status="failed", error_code="RelayError")
        assert await jobs.settle_report(db_session, task_id, "failed", "RelayError") is True
        await db_session.commit()
        later = utcnow() + timedelta(minutes=10 * (attempt + 1))
        queued = await jobs.report(db_session, bot, await reloaded(db_session, row), later)
        await db_session.commit()
        assert (queued is not None) == (attempt < jobs.MAX_REPORT_FAILURES - 1)
    final = await reloaded(db_session, row)
    assert final.reported_at is not None and final.report_code == jobs.UNDELIVERABLE
    assert final.report_failures == jobs.MAX_REPORT_FAILURES
    # 结论还在行里，助理用 assistant_job_list 读得到。
    assert final.result_enc == RESULT


async def test_a_result_turn_with_nothing_bound_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    _, row = await finished(db_session)
    turn = await take(db_session, row.result_task_id)
    row.result_task_id = None
    await db_session.commit()
    done, relay = await run(db_engine, db_session, turn, AssistantJobResultHandler())
    assert done.status == "cancelled" and done.error_code == "job_result_inactive"
    assert relay.requests == [] and await streams_of(db_session, turn) == []


async def test_nothing_is_queued_while_the_runtime_is_paused(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    row = await spawned(db_session, bot)
    assert row.last_task_id is not None
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False
    await db_session.commit()
    done = await jobs.complete(
        db_session, bot, row.id, task_id=row.last_task_id, ok=True, result=RESULT
    )
    await db_session.commit()
    assert done is not None and done.status == "done" and done.result_task_id is None
    assert await tasks_of(db_session, bot, JOB_RESULT_KIND) == []
    # 恢复之后照常投回，没有丢。
    relay.assistant_pool = True
    await db_session.commit()
    assert await jobs.report(db_session, bot, done, utcnow()) is not None
