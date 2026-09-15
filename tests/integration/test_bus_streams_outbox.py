from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, OutboxItem, User
from coreman.core.observability.metrics import OUTBOX_FAILED


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


async def _send(session, bot, key):
    return await outbox.add(
        session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key=key,
        target={"chat_id": "c"},
        payload={"markdown": key},
    )


async def _rows(session):
    return list(
        await session.scalars(
            select(OutboxItem).order_by(OutboxItem.id).execution_options(populate_existing=True)
        )
    )


async def _due(session, item_id):
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id)
        .values(not_before=func.now() - text("interval '1 second'"))
    )
    await session.commit()


async def test_abandoned_attempt_is_retried_while_the_dependent_card_waits(db_session):
    """网关在等回执时死掉：结果未知，由 scheduler 回收后按普通失败退避重发；卡片一直等着。"""
    bot = await _bot(db_session)
    bot_id = bot.id
    first = await _send(db_session, bot, "77:send:final")
    card = await _send(db_session, bot, "77:card:0")
    first_id, card_id = first.id, card.id
    # 投递组由幂等键推出，不混进要发给平台的 payload
    assert first.payload == {"markdown": "77:send:final"}
    claimed = await outbox.claim_next(db_session, bot_id=bot_id)
    assert claimed.id == first_id
    await outbox.begin_attempt(db_session, claimed, "lost-attempt")
    await _due(db_session, first_id)
    # 认领本身不再顺手判死任何东西：sending 的前序项照样挡住卡片
    assert await outbox.claim_next(db_session, bot_id=bot_id) is None
    await db_session.rollback()
    assert await outbox.recover_abandoned(db_session) == 1
    await db_session.commit()
    rows = await _rows(db_session)
    assert [(r.status, r.attempts) for r in rows] == [("pending", 1), ("pending", 0)]
    assert rows[0].last_error.startswith("delivery_unknown") and rows[1].last_error is None
    await _due(db_session, first_id)
    again = await outbox.claim_next(db_session, bot_id=bot_id)
    assert again.id == first_id
    await outbox.begin_attempt(db_session, again, "attempt-2")
    assert again.payload["_attempt_id"] == "attempt-2"
    assert await outbox.unknown_attempt(db_session, first_id, "delivery_unknown: x") == "pending"
    await db_session.commit()
    # 迟到的第二次判定不会重复计数：条目已经不是 sending
    assert await outbox.unknown_attempt(db_session, first_id, "delivery_unknown: y") is None
    await _due(db_session, first_id)
    assert (await outbox.claim_next(db_session, bot_id=bot_id)).id == first_id
    await outbox.mark_sent(db_session, first_id)
    assert (await outbox.claim_next(db_session, bot_id=bot_id)).id == card_id


async def test_permanent_failure_releases_dependents_and_retry_restores_order(db_session):
    """前序项最终失败：后续项立即可领（不再级联判死）；人工重投后顺序保证重新生效。"""
    bot = await _bot(db_session)
    bot_id = bot.id
    first = await _send(db_session, bot, "77:send:final")
    card = await _send(db_session, bot, "77:card:0")
    first_id, card_id = first.id, card.id
    await db_session.commit()
    failures = OUTBOX_FAILED._value.get()
    status = None
    for attempt in range(outbox.MAX_ATTEMPTS):
        await _due(db_session, first_id)
        claimed = await outbox.claim_next(db_session, bot_id=bot_id)
        assert claimed.id == first_id
        await outbox.begin_attempt(db_session, claimed, f"attempt-{attempt}")
        status = await outbox.unknown_attempt(db_session, first_id, "delivery_unknown: timeout")
        await db_session.commit()
    assert status == "failed"
    assert OUTBOX_FAILED._value.get() == failures + 1
    rows = await _rows(db_session)
    assert [r.status for r in rows] == ["failed", "pending"] and rows[1].last_error is None
    # 迟到的回执不得把判死的条目改回 sent
    await outbox.mark_sent(db_session, first_id)
    await db_session.commit()
    assert (await _rows(db_session))[0].status == "failed"
    # 前序项已经不可能成功：卡片不再等它（也没有被级联判死）
    assert (await outbox.claim_next(db_session, bot_id=bot_id)).id == card_id
    await db_session.rollback()
    # 人工重投之后顺序保证重新生效：卡片排回它后面
    assert await outbox.retry(db_session, first_id)
    await db_session.commit()
    assert (await outbox.claim_next(db_session, bot_id=bot_id)).id == first_id
    assert await outbox.claim_next(db_session, bot_id=bot_id) is None
    await outbox.mark_sent(db_session, first_id)
    assert (await outbox.claim_next(db_session, bot_id=bot_id)).id == card_id


async def test_notify_items_are_marked_sent_straight_from_pending(db_session):
    """通知消费者在行锁里直接从 pending 发出：`mark_sent` 必须认 pending。"""
    item = await outbox.add(
        db_session,
        bot_id=None,
        platform="wecom",
        kind="notify",
        dedupe_key="notify:1",
        target={"user": "u"},
        payload={"content": "x"},
    )
    await outbox.mark_sent(db_session, item.id)
    await db_session.commit()
    assert (await _rows(db_session))[0].status == "sent"


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
