"""task_streams：思考、正文、卡片与计划加密入库；网关取到的是明文；销毁后跳过。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
CONTENT = (
    "SELECT concat_ws('|', thinking_md, pending_text, final_text, pending_card::text, plan::text)"
    " FROM task_streams WHERE task_id = :id"
)
PLAN = [{"text": SECRET, "status": "pending"}]


async def _stream(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await session.commit()
    return task.id


async def test_stream_content_roundtrip(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    version = await streams.update(
        db_session,
        task_id,
        bot_id=bot.id,
        thinking_md=f"想：{SECRET}",
        pending_text=f"答：{SECRET}",
        segment_boundaries=[2],
        plan=PLAN,
    )
    await db_session.commit()
    assert version == 1
    stored = str(await raw(db_session, CONTENT, id=task_id))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    factory = make_session_factory(db_engine)
    async with factory() as other:
        pending = await streams.pending_for_bot(other, bot.id)
        assert [p.task_id for p in pending] == [task_id]
        assert pending[0].pending_text == f"答：{SECRET}" and pending[0].plan == PLAN
        assert pending[0].segment_boundaries == [2]
        active = await streams.active_for_bot(other, bot.id)
        assert active[0].thinking_md == f"想：{SECRET}"
    # 不传 bot_id：按主键查到这条流属于谁。
    done = await streams.complete(
        db_session, task_id, final_text=f"终：{SECRET}", pending_card={"body": SECRET}
    )
    await db_session.commit()
    assert done.version == 2
    assert SECRET not in str(await raw(db_session, CONTENT, id=task_id))
    async with factory() as other:
        row = await streams.get(other, task_id)
        assert row is not None and row.final_text == f"终：{SECRET}"
        assert row.pending_card == {"body": SECRET} and row.is_complete


async def test_metadata_only_update_needs_no_content(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(
        db_session, task_id, delivery_mode="proactive", background_state={"offset": 3}
    )
    await db_session.commit()
    stored = await raw(
        db_session,
        "SELECT background_state::text FROM task_streams WHERE task_id = :id",
        id=task_id,
    )
    assert stored == '{"offset": 3}'


async def test_destroyed_key_skips_the_stream(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, bot_id=bot.id, pending_text=SECRET, plan=PLAN)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert await streams.pending_for_bot(db_session, bot.id) == []
    assert await streams.active_for_bot(db_session, bot.id) == []
    row = await streams.get(db_session, task_id)
    assert row is not None and sealed.is_locked(row)
    assert row.pending_text == "" and row.final_text is None and row.plan == []
    # 销毁之后收尾：不抛错，终稿丢弃，流照常结束。
    done = await streams.complete(db_session, task_id, final_text=SECRET, bot_id=bot.id)
    await db_session.commit()
    assert done.version == 2
    assert await raw(
        db_session, "SELECT final_text IS NULL FROM task_streams WHERE task_id = :id", id=task_id
    )


async def test_standard_bot_stream_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, pending_text=SECRET, plan=PLAN)
    await streams.complete(db_session, task_id, final_text=SECRET)
    await db_session.commit()
    stored = str(await raw(db_session, CONTENT, id=task_id))
    assert SECRET in stored and "enc:v2:" not in stored and "_sealed" not in stored
    pending = await streams.pending_for_bot(db_session, bot.id)
    assert pending[0].final_text == SECRET
