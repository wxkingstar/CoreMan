from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, OutboxItem, User
from coreman.runtime.bus import outbox, streams, tasks
from coreman.runtime.bus.tasks import NewTask


async def test_same_task_card_cannot_overtake_retry_or_unconfirmed_result(db_session):
    bot = await _bot(db_session)

    async def add(key):
        return await outbox.add(
            db_session,
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key=key,
            target={"chat_id": "c"},
            payload={"markdown": key},
        )

    final = await add("77:send:final")
    card = await add("77:card:0")
    other = await add("78:send:final")
    final.not_before = datetime.now(UTC) + timedelta(seconds=60)
    await db_session.commit()
    assert (await outbox.claim_next(db_session, bot_id=bot.id)).id == other.id
    await outbox.mark_sent(db_session, other.id)
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None
    final.not_before = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert (await outbox.claim_next(db_session, bot_id=bot.id)).id == final.id
    await outbox.begin_attempt(db_session, final, "attempt-1")
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None
    await outbox.mark_failed(db_session, final.id, "846607")
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None
    final = await db_session.get(OutboxItem, final.id, populate_existing=True)
    final.not_before = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert (await outbox.claim_next(db_session, bot_id=bot.id)).id == final.id
    await outbox.begin_attempt(db_session, final, "attempt-2")
    await outbox.mark_sent(db_session, final.id)
    assert (await outbox.claim_next(db_session, bot_id=bot.id)).id == card.id


async def test_crashed_send_is_unknown_and_blocks_dependent_card(db_session):
    bot = await _bot(db_session)
    for key in ("77:send:final", "77:card:0"):
        await outbox.add(
            db_session,
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key=key,
            target={"chat_id": "c"},
            payload={"markdown": key},
        )
    first = await outbox.claim_next(db_session, bot_id=bot.id)
    await outbox.begin_attempt(db_session, first, "lost-attempt")
    first.not_before = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None
    rows = list(
        await db_session.scalars(
            select(OutboxItem).order_by(OutboxItem.id).execution_options(populate_existing=True)
        )
    )
    assert [r.status for r in rows] == ["failed", "failed"]
    assert rows[0].last_error.startswith("delivery_unknown")
    assert rows[1].last_error.startswith("dependency_failed")


async def _bot(session: AsyncSession) -> Bot:
    user = User(login_name="c", display_name="c")
    session.add(user)
    await session.flush()
    bot = Bot(
        bot_key="b1",
        platform="wecom",
        name="b1",
        created_by=user.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(bot)
    await session.commit()
    return bot


async def test_stream_versions_and_pushed(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    t = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u1")
    )
    assert t
    s = await streams.create(
        db_session,
        task_id=t.id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="s1",
        reply_context={"req_id": "r1"},
        lease_generation=3,
        running_since=datetime.now(UTC),
    )
    await db_session.commit()
    assert s.version == 0 and s.delivery_mode == "stream"
    assert await streams.update(db_session, t.id, thinking_md="🤔 a") == 1
    assert await streams.update(db_session, t.id, pending_text="hi", segment_boundaries=[2]) == 2
    await db_session.commit()
    pending = await streams.pending_for_bot(db_session, bot.id)
    assert (
        [p.task_id for p in pending] == [t.id]
        and pending[0].version == 2
        and pending[0].pending_text == "hi"
    )
    await streams.mark_pushed(db_session, t.id, 2)
    await db_session.commit()
    assert await streams.pending_for_bot(db_session, bot.id) == []
    done = await streams.complete(db_session, t.id, final_text="done", pending_card={"k": 1})
    assert done.version == 3 and done.delivery_mode == "stream" and done.offset == 0
    await db_session.commit()
    pending = await streams.pending_for_bot(db_session, bot.id)
    assert len(pending) == 1 and pending[0].is_complete and pending[0].final_text == "done"
    await streams.mark_pushed(db_session, t.id, 3)
    await streams.mark_finish_pushed(db_session, t.id)
    await db_session.commit()
    assert await streams.pending_for_bot(db_session, bot.id) == []
    assert await streams.active_for_bot(db_session, bot.id) == []
    row = await streams.get(db_session, t.id)
    assert row and row.completed_at is not None and row.pending_card == {"k": 1}
    await streams.mark_pushed(db_session, t.id, 1)  # 不回退
    await db_session.commit()
    row = await streams.get(db_session, t.id)
    assert row and row.pushed_version == 3


async def test_complete_reports_the_delivery_mode_of_that_moment(
    db_session: AsyncSession,
) -> None:
    """收尾那条 UPDATE 自己带回投递状态：网关先翻 proactive 就是 proactive，没翻就是 stream。

    worker 另开一个事务去读的话，网关的 drain 正好插在读与写之间时两边都不推终稿。
    """
    bot = await _bot(db_session)
    first = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u1")
    )
    second = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="u2")
    )
    assert first and second
    for t in (first, second):
        await streams.create(
            db_session,
            task_id=t.id,
            bot_id=bot.id,
            platform="wecom",
            stream_id=f"s{t.id}",
            reply_context={"req_id": "r", "chat_id": "u"},
            lease_generation=1,
            running_since=datetime.now(UTC),
        )
    await db_session.commit()
    # 网关先翻（排空时记下已经投递的前缀长度），worker 后收尾
    await streams.update(
        db_session,
        first.id,
        delivery_mode="proactive",
        background_state={"mode": "proactive", "offset": 3, "finish_suffix": "", "switched_at": ""},
    )
    await db_session.commit()
    taken = await streams.complete(db_session, first.id, final_text="abcdef")
    await db_session.commit()
    assert taken.delivery_mode == "proactive" and taken.offset == 3
    kept = await streams.complete(db_session, second.id, final_text="abcdef")
    await db_session.commit()
    assert kept.delivery_mode == "stream" and kept.offset == 0 and kept.finish_pushed_at is None


async def test_outbox_claim_backoff_and_retry(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    a = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key="1:send:0",
        target={"chat_id": "u"},
        payload={"markdown": "a"},
    )
    b = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key="1:send:1",
        target={"chat_id": "u"},
        payload={"markdown": "b"},
        not_before=datetime.now(UTC) + timedelta(hours=1),
    )
    assert a and b
    assert (
        await outbox.add(
            db_session,
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key="1:send:0",
            target={},
            payload={},
        )
        is None
    )
    await db_session.commit()
    assert await outbox.count_pending(db_session) == 2
    item = await outbox.claim_next(db_session, bot_id=bot.id)
    assert item and item.id == a.id and item.status == "sending"
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None  # b 未到 not_before
    assert await outbox.mark_failed(db_session, a.id, "boom") == "pending"
    await db_session.commit()
    row = (await db_session.execute(select(OutboxItem).where(OutboxItem.id == a.id))).scalar_one()
    assert (
        row.attempts == 1
        and row.last_error == "boom"
        and row.not_before > datetime.now(UTC) + timedelta(seconds=1)
    )
    for _ in range(5):
        row.not_before = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        got = await outbox.claim_next(db_session, bot_id=bot.id)
        assert got and got.id == a.id
        status = await outbox.mark_failed(db_session, a.id, "boom")
        await db_session.commit()
        await db_session.refresh(row)
    assert status == "failed" and row.attempts == 6 and row.status == "failed"
    assert [x.id for x in await outbox.list_failed(db_session)] == [a.id]
    assert await outbox.retry(db_session, a.id) is True
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "pending" and row.attempts == 0
    got = await outbox.claim_next(db_session, bot_id=bot.id)
    assert got and got.id == a.id
    await outbox.mark_sent(db_session, a.id)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "sent" and row.sent_at is not None
    assert await outbox.retry(db_session, a.id) is False
    await outbox.mark_skipped(db_session, b.id, "stale generation")
    await db_session.commit()
    brow = (await db_session.execute(select(OutboxItem).where(OutboxItem.id == b.id))).scalar_one()
    assert brow.status == "skipped" and brow.last_error == "stale generation"
