"""派出一次求助：助理这边是密文，目标员工那边只有编号；上限与子任务共用；每次都记审计。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, dispatch, jobs
from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.db.models import AuditLog
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import spend
from tests.integration.dispatch_helpers import QUESTION, asked, target_bot
from tests.integration.job_helpers import spawned, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT title_enc || brief_enc || coalesce(result_enc, '') FROM assistant_jobs WHERE id = :id"
)


async def _undo(session: AsyncSession, *rows: object) -> None:
    """被拒之后回滚，并把用例里还要用的行重新读出来（回滚让它们过期）。"""
    await session.rollback()
    for row in rows:
        await session.refresh(row)


async def test_the_row_is_sealed_and_the_target_task_carries_ids_only(
    db_session: AsyncSession,
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "库存" not in stored and stored.count("enc:v2:d1:") == 2
    assert (row.kind, row.origin, row.status, row.rounds) == ("bot_help", "assistant", "queued", 1)
    assert (row.bot_id, row.target_bot_id) == (bot.id, target.id)
    assert row.session_key == f"assistant-help:{row.id}" and row.origin_session_key == "oc_private"
    assert row.brief_enc == QUESTION and row.title_enc == "请「demo-bot」协助"
    (task,) = await tasks_of(db_session, target, BOT_HELP_KIND)
    assert task.id == row.last_task_id and task.status == "queued"
    assert (task.bot_id, task.user_id, task.session_key) == (target.id, owner.id, row.session_key)
    assert task.inbound_event_id is None
    assert task.payload == {
        "job_id": str(row.id),
        "round": 1,
        "allow_write": False,
        "bot_key": "demo-bot",
        "platform_user_id": "owner_pid",
    }
    # 助理自己名下没有这一轮的任务。
    assert await tasks_of(db_session, bot, BOT_HELP_KIND) == []


async def test_every_dispatch_is_audited_without_the_question(db_session: AsyncSession) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    first = await asked(db_session, bot, target)
    second = await asked(db_session, bot, target, question="再查一下退货", attended=False)
    logs = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == dispatch.AUDIT_ACTION).order_by(AuditLog.id)
        )
    )
    assert [(log.actor_id, log.target_type, log.target_id) for log in logs] == [
        (owner.id, "bot", str(target.id))
    ] * 2
    assert logs[0].actor_login == owner.login_name
    assert logs[0].diff == {
        "assistant_bot_id": str(bot.id),
        "job_id": str(first.id),
        "unattended": False,
        "allow_write": False,
        "approved": False,
        "question_chars": len(QUESTION),
    }
    assert logs[1].diff["unattended"] is True and logs[1].diff["job_id"] == str(second.id)
    assert SECRET not in str([log.diff for log in logs]) and "退货" not in str(logs[1].diff)


async def test_a_question_that_is_too_long_leaves_nothing_behind(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.send(
            db_session,
            bot,
            target,
            "问" * (dispatch.MAX_QUESTION + 1),
            allow_write=False,
            attended=True,
        )
    assert refused.value.code == "question_too_long"
    await db_session.rollback()
    assert await tasks_of(db_session, target, BOT_HELP_KIND) == []
    assert await raw(db_session, "SELECT count(*) FROM assistant_jobs") == 0
    assert await raw(db_session, "SELECT count(*) FROM audit_logs") == 0


async def test_dispatches_and_subtasks_share_the_three_slots(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    await spawned(db_session, bot)
    await spawned(db_session, bot, title="查合同")
    third = await asked(db_session, bot, target)
    with pytest.raises(jobs.JobError) as full:
        await dispatch.send(db_session, bot, target, "还有一件", allow_write=False, attended=True)
    assert full.value.code == "too_many_jobs"
    await _undo(db_session, bot, target, third)
    # 取消一个之后名额回来。
    await jobs.cancel(db_session, bot.id, third.id)
    await db_session.commit()
    await asked(db_session, bot, target, question="还有一件")


async def test_dispatches_count_towards_the_daily_twenty(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    for index in range(jobs.MAX_DAILY):
        row = await asked(db_session, bot, target, question=f"第 {index} 件")
        await jobs.cancel(db_session, bot.id, row.id)
        await db_session.commit()
    with pytest.raises(jobs.JobError) as refused:
        await dispatch.send(db_session, bot, target, "第 21 件", allow_write=False, attended=True)
    assert refused.value.code == "daily_job_limit"


async def test_an_unattended_turn_cannot_dispatch_once_the_budget_is_spent(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    await spend(db_session, bot, delivery.DEFAULT_BUDGET, datetime.now(UTC))
    with pytest.raises(jobs.JobError) as refused:
        await dispatch.send(db_session, bot, target, QUESTION, allow_write=False, attended=False)
    assert refused.value.code == "daily_budget_exhausted"
    await _undo(db_session, bot, target)
    # 所有者在场、或所有者刚批准的派出不看预算。
    await asked(db_session, bot, target)
    await asked(db_session, bot, target, question="批准过的", attended=False, approved=True)


async def test_a_given_id_makes_the_dispatch_idempotent(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    job_id = uuid.uuid4()
    row = await asked(db_session, bot, target, allow_write=True, approved=True, job_id=job_id)
    assert row.id == job_id
    (task,) = await tasks_of(db_session, target, BOT_HELP_KIND)
    assert task.payload["allow_write"] is True
    assert task.dedupe_key == f"assistant-help:{job_id}:round:1"
    with pytest.raises(dispatch.DispatchError) as again:
        await dispatch.send(
            db_session, bot, target, QUESTION, allow_write=True, attended=True, job_id=job_id
        )
    assert again.value.code == "already_dispatched"
    await _undo(db_session, bot, target)
    assert len(await tasks_of(db_session, target, BOT_HELP_KIND)) == 1
