"""assistant_jobs：约束、默认值、内容列在库里是密文，以及待跟进指向子任务的外键。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantFollowup, AssistantJob
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT title_enc || brief_enc || coalesce(message_enc, '') || coalesce(result_enc, '')"
    " || coalesce(blocked_enc, '') FROM assistant_jobs WHERE id = :id"
)


def _row(bot_id: uuid.UUID, **kw: object) -> AssistantJob:
    job_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": job_id,
        "bot_id": bot_id,
        "title_enc": f"查李四的报价 {SECRET}",
        "brief_enc": f"翻一下上周的聊天记录 {SECRET}",
        "session_key": f"oc_private#job:{job_id}",
        "origin_session_key": "oc_private",
        "deadline_at": utcnow() + timedelta(hours=2),
    }
    return AssistantJob(**{**values, **kw})


def test_table_is_truncated_with_the_other_business_tables() -> None:
    assert "assistant_jobs" in BUSINESS_TABLES


async def test_content_is_sealed_and_metadata_is_plain(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _row(
        bot.id,
        message_enc=f"再看看合同 {SECRET}",
        result_enc=f"报价 3 万 {SECRET}",
        blocked_enc=f"要所有者确认 {SECRET}",
    )
    db_session.add(row)
    await db_session.commit()
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "李四" not in stored and stored.count("enc:v2:d1:") == 5
    plain = await raw(
        db_session,
        "SELECT kind || ':' || origin || ':' || status || ':' || rounds::text || ':'"
        " || report::text || ':' || report_seq::text || ':' || report_failures::text"
        " FROM assistant_jobs WHERE id = :id",
        id=row.id,
    )
    assert plain == "subtask:assistant:queued:0:true:0:0"
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantJob, row.id)
        assert found is not None and found.title_enc == f"查李四的报价 {SECRET}"
        assert found.result_enc == f"报价 3 万 {SECRET}"
        assert found.last_task_id is None and found.reported_at is None
        # 密钥销毁之后：行还在、元数据可读，内容为空且不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantJob, row.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert (locked.title_enc, locked.brief_enc, locked.result_enc) == ("", "", None)
        assert locked.session_key.endswith(f"#job:{row.id}")


async def test_kind_origin_and_status_are_constrained(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    bot_id = bot.id
    for bad in ({"kind": "errand"}, {"origin": "owner"}, {"status": "paused"}):
        db_session.add(_row(bot_id, **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
    db_session.add(_row(bot_id, kind="bot_help", origin="system", status="blocked"))
    await db_session.flush()


async def test_a_followup_forgets_a_deleted_job(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    job = _row(bot.id)
    db_session.add(job)
    await db_session.flush()
    followup = AssistantFollowup(
        bot_id=bot.id, seq=1, category="waiting", title_enc="等子任务的结论", job_id=job.id
    )
    db_session.add(followup)
    await db_session.commit()
    # 回滚会让已加载的对象过期，编号先取出来。
    bot_id, job_id, followup_id = bot.id, job.id, followup.id
    # 指向不存在的子任务进不去。
    db_session.add(
        AssistantFollowup(
            bot_id=bot_id, seq=2, category="waiting", title_enc="x", job_id=uuid.uuid4()
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    await db_session.delete(await db_session.get(AssistantJob, job_id))
    await db_session.commit()
    kept = await db_session.get(AssistantFollowup, followup_id, populate_existing=True)
    assert kept is not None and kept.job_id is None
