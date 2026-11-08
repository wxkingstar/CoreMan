"""排队的消息合成一轮处理；等太久的先发卡片确认。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.chat import busy_choice
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, Task
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import run
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _say(
    session: AsyncSession,
    bot: Bot,
    text: str | None = "",
    *,
    user: str = "zs",
    chat: str = "group",
    parts: list[dict] | None = None,
) -> Task:
    mid = f"m-{uuid.uuid4()}"
    message = InboundMessage(
        platform=bot.platform,
        bot_id=bot.id,
        kind="message",
        chat_type="group",
        chat_id=chat,
        sender={"platform_user_id": user},
        message_id=mid,
        parts=parts or [{"type": "text", "text": text}],
        reply_context={"chat_id": chat, "chat_type": "group", "message_id": mid},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    assert task is not None
    return task


async def _click(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, target: Task, key: str
) -> None:
    message = InboundMessage(
        platform=bot.platform,
        bot_id=bot.id,
        kind="card_action",
        chat_type="group",
        chat_id="group",
        sender={"platform_user_id": "zs"},
        message_id=f"a-{uuid.uuid4()}",
        card_action={
            "task_id": busy_choice.card_task_id(target.id),
            "card_type": "button_interaction",
            "event_key": key,
        },
        reply_context={"chat_id": "group", "chat_type": "group", "req_id": "r1"},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    assert await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    claimed = await _claim(session, "fast")
    assert claimed
    await CardActionHandler().run(build_ctx(engine, claimed))


async def _claim(session: AsyncSession, lane: str = "normal") -> Task | None:
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane=lane, instance_id="worker-test")
    await session.commit()
    return claimed


async def _done(session: AsyncSession, task: Task) -> None:
    await tasks.finish(session, task.id, status="succeeded")
    await session.commit()


async def _age(session: AsyncSession, task: Task, minutes: int) -> None:
    await session.execute(
        update(InboundEvent)
        .where(InboundEvent.id == task.inbound_event_id)
        .values(received_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await session.commit()


async def _row(session: AsyncSession, task: Task) -> Task:
    row = await session.get(Task, task.id, populate_existing=True)
    assert row is not None
    return row


def _sent(relay: FakeRelay) -> str:
    return json.dumps(relay.requests[-1], ensure_ascii=False)


async def test_queued_messages_become_one_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "先做这个")
    second = await _say(db_session, bot, "现在什么进展")
    third = await _say(db_session, bot, "现在什么进展")
    fourth = await _say(db_session, bot, "推荐码也删除")
    await _done(db_session, first)

    claimed = await _claim(db_session)
    assert claimed and claimed.id == second.id
    relay = FakeRelay("normal")
    await run(db_engine, claimed, relay)

    assert len(relay.requests) == 1
    sent = _sent(relay)
    assert "现在什么进展" in sent and "推荐码也删除" in sent and "以最新的为准" in sent
    for merged in (third, fourth):
        row = await _row(db_session, merged)
        assert (row.status, row.error_code) == ("cancelled", "merged")
        assert row.result == {"merged_into": second.id}
    assert (await _row(db_session, second)).status == "succeeded"
    assert await _claim(db_session) is None


async def test_a_single_queued_message_is_left_as_it_was(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "先做这个")
    await _say(db_session, bot, "接着做那个")
    await _done(db_session, first)
    relay = FakeRelay("normal")
    claimed = await _claim(db_session)
    assert claimed
    await run(db_engine, claimed, relay)
    assert "以最新的为准" not in _sent(relay)


async def test_someone_elses_message_is_not_run_with_this_persons_identity(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "先做这个")
    second = await _say(db_session, bot, "第二条")
    other = await _say(db_session, bot, "别人的话", user="ls")
    after = await _say(db_session, bot, "第三条")
    await _done(db_session, first)
    relay = FakeRelay("normal")
    claimed = await _claim(db_session)
    assert claimed and claimed.id == second.id
    await run(db_engine, claimed, relay)
    assert "别人的话" not in _sent(relay) and "第三条" not in _sent(relay)
    assert (await _row(db_session, other)).status == "queued"
    assert (await _row(db_session, after)).status == "queued"


async def test_an_attachment_stays_for_its_own_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "先做这个")
    await _say(db_session, bot, "第二条")
    image = await _say(db_session, bot, parts=[{"type": "image", "ref": {"url": "x"}}])
    await _done(db_session, first)
    claimed = await _claim(db_session)
    assert claimed
    await run(db_engine, claimed, FakeRelay("normal"))
    assert (await _row(db_session, image)).status == "queued"


async def _held(
    engine: AsyncEngine, session: AsyncSession, bot: Bot
) -> tuple[Task, Task, FakeRelay]:
    first = await _say(session, bot, "先做这个")
    second = await _say(session, bot, "现在什么进展")
    third = await _say(session, bot, "推荐码也删除")
    await _age(session, second, 25)
    await _done(session, first)
    relay = FakeRelay("normal")
    claimed = await _claim(session)
    assert claimed and claimed.id == second.id
    await run(engine, claimed, relay)
    return second, third, relay


async def test_a_stale_batch_asks_first_and_does_not_start(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    head, third, relay = await _held(db_engine, db_session, bot)

    assert relay.requests == []
    row = await _row(db_session, head)
    assert row.status == "queued" and row.payload["stale_hold"] is True
    assert row.run_after > datetime.now(UTC) + timedelta(hours=1)
    assert (await _row(db_session, third)).error_code == "merged"
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"task:{head.id}:stale_hold:%"))
    )
    assert card is not None
    text = json.dumps(card.payload["card"], ensure_ascii=False)
    assert "现在什么进展" in text and "推荐码也删除" in text
    assert await _claim(db_session) is None


async def test_resume_runs_the_whole_batch_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    head, _third, relay = await _held(db_engine, db_session, bot)
    await _click(db_engine, db_session, bot, head, busy_choice.RESUME)

    claimed = await _claim(db_session)
    assert claimed and claimed.id == head.id
    await run(db_engine, claimed, relay)
    assert len(relay.requests) == 1
    sent = _sent(relay)
    assert "25 分钟前" in sent and "推荐码也删除" in sent
    assert (await _row(db_session, head)).status == "succeeded"


async def test_drop_cancels_without_running(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    head, _third, relay = await _held(db_engine, db_session, bot)
    await _click(db_engine, db_session, bot, head, busy_choice.DROP)
    row = await _row(db_session, head)
    assert (row.status, row.error_code) == ("cancelled", "user_dropped")
    assert await _claim(db_session) is None
    assert relay.requests == []


async def test_a_new_message_counts_as_the_answer(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    head, _third, relay = await _held(db_engine, db_session, bot)
    newest = await _say(db_session, bot, "改成只删飞书导入")

    claimed = await _claim(db_session)
    assert claimed and claimed.id == head.id
    await run(db_engine, claimed, relay)
    assert "改成只删飞书导入" in _sent(relay) and "推荐码也删除" in _sent(relay)
    assert (await _row(db_session, newest)).error_code == "merged"


async def test_unanswered_hold_expires(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _r, _c = await seed_bot(db_session)
    head, _third, relay = await _held(db_engine, db_session, bot)
    await db_session.execute(
        update(Task).where(Task.id == head.id).values(run_after=datetime.now(UTC))
    )
    await db_session.commit()
    claimed = await _claim(db_session)
    assert claimed and claimed.id == head.id
    await run(db_engine, claimed, relay)
    row = await _row(db_session, head)
    assert (row.status, row.error_code) == ("cancelled", "stale_expired")
    assert relay.requests == []


async def test_a_hold_does_not_block_other_people(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    await _held(db_engine, db_session, bot)
    other = await _say(db_session, bot, "别人的新问题", user="ls")
    claimed = await _claim(db_session)
    assert claimed and claimed.id == other.id


async def test_choosing_to_wait_skips_the_question(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "先做这个")
    second = await _say(db_session, bot, "接着做第二批")
    await _click(db_engine, db_session, bot, second, busy_choice.WAIT)
    await _age(db_session, second, 25)
    await _done(db_session, first)
    relay = FakeRelay("normal")
    claimed = await _claim(db_session)
    assert claimed and claimed.id == second.id
    await run(db_engine, claimed, relay)
    assert len(relay.requests) == 1


def test_progress_says_how_long_it_has_been_quiet() -> None:
    text = busy_choice.progress_text(
        status="running",
        seconds=2460,
        thinking_md="🔧 a",
        reply_text="",
        quiet_seconds=1800,
    )
    assert "30 分 0 秒" in text and "没有新动作" in text
