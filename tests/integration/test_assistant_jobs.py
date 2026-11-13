"""子任务的派出、追问与读取：内容是密文；别的助理够不着；同时 3 个、每天 20 个、预算。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import jobs, platform_turns
from coreman.core.assistants.kinds import JOB_RESULT_KIND, SUBTASK_KIND
from coreman.core.db.models import Bot, Task
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import spend
from tests.integration.job_helpers import (
    BRIEF,
    TITLE,
    owner_with_key,
    reloaded,
    spawned,
    tasks_of,
)

pytestmark = pytest.mark.usefixtures("assistant_master")


async def code_of(awaitable: object) -> str:
    with pytest.raises(jobs.JobError) as err:
        await awaitable  # type: ignore[misc]
    return err.value.code


def test_session_keys_carry_the_job_id() -> None:
    row_id = jobs.parse_id("0b0f6f0c-9f43-4c50-8f0e-3b2b3a8f7a11")
    key = jobs.session_key_for("oc_private", row_id)
    assert key == "oc_private#job:0b0f6f0c-9f43-4c50-8f0e-3b2b3a8f7a11"
    assert jobs.job_id_of(key) == row_id
    for other in ("oc_private", "oc_private#job:", "oc_private#job:not-a-uuid", "", None):
        assert jobs.job_id_of(other) is None


async def test_spawn_stores_ciphertext_and_queues_one_round(db_session: AsyncSession) -> None:
    bot, owner = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    assert (row.status, row.rounds, row.kind, row.origin, row.report) == (
        "queued",
        1,
        "subtask",
        "assistant",
        True,
    )
    assert row.session_key == f"oc_private#job:{row.id}"
    assert row.origin_session_key == "oc_private"
    assert row.deadline_at - row.created_at == jobs.LIFETIME
    stored = str(
        await raw(
            db_session,
            "SELECT title_enc || brief_enc FROM assistant_jobs WHERE id = :id",
            id=row.id,
        )
    )
    assert SECRET not in stored and "李四" not in stored and stored.count("enc:v2:d1:") == 2
    (task,) = await tasks_of(db_session, bot, SUBTASK_KIND)
    assert task.id == row.last_task_id and task.session_key == row.session_key
    assert task.user_id == owner.id and task.inbound_event_id is None
    assert task.dedupe_key == f"assistant-job:{row.id}:round:1"
    # 任务载荷里只有编号，没有标题与交代。
    payload = str(
        await raw(db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id)
    )
    assert SECRET not in payload and "李四" not in payload and "报价" not in payload
    opened = await db_session.get(Task, task.id, populate_existing=True)
    assert opened is not None
    assert opened.payload["job_id"] == str(row.id) and opened.payload["round"] == 1


async def test_title_and_brief_are_validated(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    for kw, code in (
        ({"title": "   "}, "invalid_title"),
        ({"title": "长" * 201}, "invalid_title"),
        ({"brief": ""}, "invalid_brief"),
        ({"brief": "长" * 8001}, "invalid_brief"),
    ):
        made = jobs.spawn(db_session, bot, **{"title": TITLE, "brief": BRIEF, **kw})
        assert await code_of(made) == code
    assert await tasks_of(db_session, bot, SUBTASK_KIND) == []


async def test_at_most_three_at_once(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    rows = [await spawned(db_session, bot) for _ in range(3)]
    assert await code_of(jobs.spawn(db_session, bot, title=TITLE, brief=BRIEF)) == "too_many_jobs"
    assert len(await tasks_of(db_session, bot, SUBTASK_KIND)) == 3
    # 结束一个，名额就空出来。
    rows[0].status = "done"
    await db_session.commit()
    await spawned(db_session, bot)


async def test_two_concurrent_spawns_cannot_both_take_the_last_slot(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await owner_with_key(db_session)
    for _ in range(2):
        await spawned(db_session, bot)
    factory = make_session_factory(db_engine)

    async def one() -> str:
        async with factory() as session:
            mine = await session.get(Bot, bot.id)
            assert mine is not None
            try:
                await jobs.spawn(session, mine, title=TITLE, brief=BRIEF)
            except jobs.JobError as exc:
                await session.rollback()
                return exc.code
            await session.commit()
            return "ok"

    assert sorted(await asyncio.gather(one(), one())) == ["ok", "too_many_jobs"]


async def test_twenty_a_day_in_the_owners_time_zone(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    morning = datetime(2026, 11, 13, 2, 0, tzinfo=UTC)  # 上海 10:00
    for _ in range(jobs.MAX_DAILY):
        row = await spawned(db_session, bot, now=morning)
        row.status = "done"
        await db_session.commit()
    later = morning + timedelta(hours=1)
    made = jobs.spawn(db_session, bot, title=TITLE, brief=BRIEF, now=later)
    assert await code_of(made) == "daily_job_limit"
    # 平台派的（下班编译）不占这 20 个。
    system = await spawned(db_session, bot, origin="system", report=False, now=later)
    system.status = "done"
    await db_session.commit()
    # 上海的第二天（UTC 16:00 之后）又可以了。
    await spawned(db_session, bot, now=datetime(2026, 11, 13, 16, 0, 1, tzinfo=UTC))


async def test_unattended_and_system_spawns_need_budget(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    now = utcnow()
    await spend(db_session, bot, 200, now)
    for kw in ({"attended": False}, {"origin": "system", "report": False}):
        made = jobs.spawn(db_session, bot, title=TITLE, brief=BRIEF, now=now, **kw)  # type: ignore[arg-type]
        assert await code_of(made) == "daily_budget_exhausted"
    # 所有者在场的轮次里派出不看预算。
    await spawned(db_session, bot, now=now)


async def test_another_assistant_cannot_see_or_touch_the_job(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    other, _ = await owner_with_key(
        db_session, chat_id="oc_other", platform_user_id="other_pid", open_id="ou_other"
    )
    row = await spawned(db_session, bot)
    row.status, row.result_enc = "done", "报价 3 万"
    await db_session.commit()
    assert await code_of(jobs.get(db_session, other.id, row.id)) == "job_not_found"
    assert await jobs.list_(db_session, other.id, scope="recent") == []
    assert await code_of(jobs.message(db_session, other, row.id, "再查查")) == "job_not_found"
    (mine,) = await jobs.list_(db_session, bot.id, scope="recent")
    assert mine.title_enc == TITLE and jobs.view(mine, full=True)["result"] == "报价 3 万"
    assert await tasks_of(db_session, other, SUBTASK_KIND) == []


async def test_listing_by_scope_and_the_view(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    running = await spawned(db_session, bot, title="在跑的")
    stuck = await spawned(db_session, bot, title="卡住的")
    stuck.status, stuck.blocked_enc = "blocked", "要所有者确认"
    await db_session.commit()
    assert [row.id for row in await jobs.list_(db_session, bot.id)] == [running.id]
    assert [row.id for row in await jobs.list_(db_session, bot.id, scope="blocked")] == [stuck.id]
    assert len(await jobs.list_(db_session, bot.id, scope="recent")) == 2
    assert await code_of(jobs.list_(db_session, bot.id, scope="everything")) == "invalid_scope"
    brief = jobs.view(running)
    assert brief["id"] == str(running.id) and brief["title"] == "在跑的"
    assert (brief["status"], brief["rounds"], brief["has_result"]) == ("queued", 1, False)
    assert "brief" not in brief and "result" not in brief
    full = jobs.view(await jobs.get(db_session, bot.id, stuck.id), full=True)
    assert full["brief"] == BRIEF and full["blocked_reason"] == "要所有者确认"


async def test_a_follow_up_queues_the_next_round_in_the_same_session(
    db_session: AsyncSession,
) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    row.status, row.result_enc, row.blocked_enc = "blocked", "报价 3 万", "要不要继续"
    await db_session.commit()
    again = await jobs.message(db_session, bot, row.id, f"合同里怎么写的？{SECRET}")
    await db_session.commit()
    assert (again.status, again.rounds, again.session_key) == ("queued", 2, row.session_key)
    assert again.message_enc == f"合同里怎么写的？{SECRET}" and again.blocked_enc is None
    # 上一轮的结论留着，直到新的一轮写下新的。
    assert again.result_enc == "报价 3 万" and again.finished_at is None
    first, second = await tasks_of(db_session, bot, SUBTASK_KIND)
    assert first.session_key == second.session_key == row.session_key
    assert second.dedupe_key == f"assistant-job:{row.id}:round:2"
    assert again.last_task_id == second.id
    stored = str(
        await raw(db_session, "SELECT message_enc FROM assistant_jobs WHERE id = :id", id=row.id)
    )
    assert SECRET not in stored and "合同" not in stored


async def test_follow_ups_are_refused_when_busy_closed_or_out_of_rounds(
    db_session: AsyncSession,
) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    assert await code_of(jobs.message(db_session, bot, row.id, "快点")) == "job_busy"
    row.status = "failed"
    await db_session.commit()
    assert await code_of(jobs.message(db_session, bot, row.id, "再来")) == "job_closed"
    row.status, row.rounds = "done", jobs.MAX_ROUNDS
    await db_session.commit()
    assert await code_of(jobs.message(db_session, bot, row.id, "第七轮")) == "job_round_limit"
    assert await code_of(jobs.message(db_session, bot, row.id, "  ")) == "invalid_text"
    # 轮数超限不把已有结论的子任务改成失败。
    assert (await reloaded(db_session, row)).status == "done"
    row.rounds = jobs.MAX_ROUNDS - 1
    await db_session.commit()
    last = await jobs.message(db_session, bot, row.id, "第六轮")
    assert last.rounds == jobs.MAX_ROUNDS and last.status == "queued"


async def test_a_follow_up_withdraws_a_result_still_on_its_way(db_session: AsyncSession) -> None:
    bot, _ = await owner_with_key(db_session)
    row = await spawned(db_session, bot)
    result = await platform_turns.enqueue(
        db_session, bot, JOB_RESULT_KIND, {"job_id": str(row.id)}, dedupe_key="demo-result"
    )
    assert result is not None
    row.status, row.result_enc, row.result_task_id, row.report_seq = "done", "x", result.id, 1
    await db_session.commit()
    again = await jobs.message(db_session, bot, row.id, "再看看合同")
    await db_session.commit()
    assert again.result_task_id is None and again.reported_at is None
    stale = await db_session.get(Task, result.id, populate_existing=True)
    assert stale is not None and stale.cancel_requested_at is not None
