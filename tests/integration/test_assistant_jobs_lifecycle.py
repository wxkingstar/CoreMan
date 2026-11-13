"""子任务的生命周期：只认在途的那一轮；卡住、取消、收尾各自只走一次。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs, keys, sealed
from coreman.core.db.models import AssistantJob, Bot, Task
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")


async def turn_of(session: AsyncSession, row: AssistantJob) -> Task:
    """子任务在途那一轮的任务行（载荷已还原）。"""
    assert row.last_task_id is not None
    task = await session.get(Task, row.last_task_id, populate_existing=True)
    assert task is not None
    return task


async def code_of(awaitable: object) -> str:
    with pytest.raises(jobs.JobError) as err:
        await awaitable  # type: ignore[misc]
    return err.value.code


async def two(session: AsyncSession) -> tuple[Bot, Bot]:
    bot, _ = await owner_with_key(session)
    other, _ = await owner_with_key(
        session, chat_id="oc_other", platform_user_id="other_pid", open_id="ou_other"
    )
    return bot, other


async def test_a_round_finds_its_own_job_only(db_session: AsyncSession) -> None:
    bot, other = await two(db_session)
    row = await spawned(db_session, bot)
    task = await turn_of(db_session, row)
    found = await jobs.for_round(db_session, bot.id, task)
    assert found is not None and found.id == row.id and found.brief_enc.endswith(SECRET)
    # 别的助理拿着这个任务也找不到。
    assert await jobs.for_round(db_session, other.id, task) is None
    # 已经结束的子任务、不是在途的那一轮：都没有这一轮。
    second = await spawned(db_session, bot)
    assert await jobs.for_round(db_session, bot.id, await turn_of(db_session, second)) is not None
    row.last_task_id = second.last_task_id
    await db_session.commit()
    assert await jobs.for_round(db_session, bot.id, task) is None
    second.status = "cancelled"
    await db_session.commit()
    assert await jobs.for_round(db_session, bot.id, await turn_of(db_session, second)) is None


async def test_running_is_marked_once_by_the_round_in_flight(db_session: AsyncSession) -> None:
    bot, other = await two(db_session)
    row = await spawned(db_session, bot)
    task = await turn_of(db_session, row)
    assert await jobs.mark_running(db_session, other.id, task) is False
    assert await jobs.mark_running(db_session, bot.id, task) is True
    await db_session.commit()
    fresh = await reloaded(db_session, row)
    assert fresh.status == "running" and fresh.started_at is not None
    assert await jobs.mark_running(db_session, bot.id, task) is False


async def test_blocking_stores_the_reason_sealed(db_session: AsyncSession) -> None:
    bot, other = await two(db_session)
    row = await spawned(db_session, bot)
    task = await turn_of(db_session, row)
    assert await code_of(jobs.block(db_session, other.id, task, "x")) == "job_not_found"
    assert await code_of(jobs.block(db_session, bot.id, task, " ")) == "invalid_reason"
    assert await code_of(jobs.block(db_session, bot.id, task, "长" * 1001)) == "invalid_reason"
    await jobs.block(db_session, bot.id, task, f"要所有者确认预算 {SECRET}")
    await db_session.commit()
    stored = str(
        await raw(db_session, "SELECT blocked_enc FROM assistant_jobs WHERE id = :id", id=row.id)
    )
    assert SECRET not in stored and "预算" not in stored
    # 状态要等这一轮结束才变。
    assert (await reloaded(db_session, row)).status == "queued"


async def test_completing_writes_the_result_once(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    task_id = row.last_task_id
    assert task_id is not None
    # 不是在途的那一轮：什么都不改。
    assert await jobs.complete(db_session, bot, row.id, task_id=task_id + 999, ok=True) is None
    done = await jobs.complete(
        db_session, bot, row.id, task_id=task_id, ok=True, result=f"  报价 3 万 {SECRET}  "
    )
    await db_session.commit()
    assert done is not None and done.status == "done" and done.finished_at is not None
    assert done.result_enc == f"报价 3 万 {SECRET}" and done.error_code is None
    stored = str(
        await raw(db_session, "SELECT result_enc FROM assistant_jobs WHERE id = :id", id=row.id)
    )
    assert SECRET not in stored and "报价" not in stored
    # 同一轮再收尾一次：没有第二次。
    assert await jobs.complete(db_session, bot, row.id, task_id=task_id, ok=False) is None
    assert (await reloaded(db_session, row)).status == "done"


async def test_a_blocked_round_ends_blocked_and_long_results_are_cut(
    db_session: AsyncSession,
) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    task = await turn_of(db_session, row)
    await jobs.block(db_session, bot.id, task, "要所有者确认预算")
    done = await jobs.complete(
        db_session, bot, row.id, task_id=task.id, ok=True, result="字" * (jobs.MAX_RESULT + 5)
    )
    await db_session.commit()
    assert done is not None and done.status == "blocked"
    assert done.result_enc is not None and len(done.result_enc) == jobs.MAX_RESULT
    empty = await spawned(db_session, bot)
    assert empty.last_task_id is not None
    out = await jobs.complete(db_session, bot, empty.id, task_id=empty.last_task_id, ok=True)
    assert out is not None and out.result_enc == jobs.EMPTY_RESULT


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("RelayError", "RelayError"),
        ("superseded", "superseded"),
        (None, "turn_failed"),
        ("节点返回了一段带内容的报错", "turn_failed"),
    ],
)
async def test_a_failed_round_keeps_only_a_code(
    db_session: AsyncSession, given: str | None, stored: str
) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    assert row.last_task_id is not None
    done = await jobs.complete(
        db_session, bot, row.id, task_id=row.last_task_id, ok=False, error_code=given
    )
    assert done is not None and (done.status, done.error_code) == ("failed", stored)
    assert done.result_enc is None


async def test_a_result_cannot_be_stored_once_the_key_is_gone(db_session: AsyncSession) -> None:
    bot, owner = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    assert row.last_task_id is not None
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    # 别的进程要等缓存过期才知道密钥没了；这里直接清掉本进程的缓存。
    keys.forget()
    sealed.forget()
    done = await jobs.complete(
        db_session, bot, row.id, task_id=row.last_task_id, ok=True, result="报价 3 万"
    )
    await db_session.commit()
    assert done is not None and (done.status, done.error_code) == ("failed", "content_locked")


async def test_cancel_stops_the_round_and_is_final(db_session: AsyncSession) -> None:
    bot, other = await two(db_session)
    row = await spawned(db_session, bot)
    assert await code_of(jobs.cancel(db_session, other.id, row.id)) == "job_not_found"
    out = await jobs.cancel(db_session, bot.id, row.id, code="cancelled_by_assistant")
    await db_session.commit()
    assert (out.status, out.error_code) == ("cancelled", "cancelled_by_assistant")
    # 取消它的人已经知道了：不投回。
    assert out.reported_at is not None and out.report_code == "not_needed"
    task = await turn_of(db_session, out)
    assert task.cancel_requested_at is not None
    assert await code_of(jobs.cancel(db_session, bot.id, row.id)) == "job_closed"
    # 被叫停的那一轮收尾时不再改它。
    assert await jobs.complete(db_session, bot, row.id, task_id=task.id, ok=False) is None
    told = await spawned(db_session, bot)
    kept = await jobs.cancel(db_session, bot.id, told.id, report=True)
    assert kept.reported_at is None and kept.report_code is None


async def test_cancel_all_takes_this_assistants_live_jobs_only(db_session: AsyncSession) -> None:
    bot, other = await two(db_session)
    mine = [await spawned(db_session, bot) for _ in range(2)]
    finished = await spawned(db_session, bot)
    finished.status = "done"
    theirs = await spawned(db_session, other)
    await db_session.commit()
    assert await jobs.cancel_all(db_session, bot.id, "assistant_deprovisioned") == 2
    await db_session.commit()
    for row in mine:
        fresh = await reloaded(db_session, row)
        assert (fresh.status, fresh.error_code) == ("cancelled", "assistant_deprovisioned")
        assert fresh.reported_at is not None
    assert (await reloaded(db_session, finished)).status == "done"
    assert (await reloaded(db_session, theirs)).status == "queued"
