"""投递：到点才交回、同一时刻至多一轮、正常结束才记已交回、预算用完后的三种级别、暂停不入队。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import delivery, settings
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantInbox, Bot, RelayServer, Task
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import boxed, handovers, inbox_rows, ready, spend

pytestmark = pytest.mark.usefixtures("assistant_master")
NOTICES = "SELECT count(*) FROM outbox WHERE bot_id = :bot AND dedupe_key LIKE 'assistant-budget:%'"


async def send(session: AsyncSession, bot: Bot, now: object = None) -> Task | None:
    task = await delivery.dispatch(session, bot, now or utcnow())  # type: ignore[arg-type]
    await session.commit()
    return task


async def end(session: AsyncSession, task: Task, status: str, code: str | None = None) -> None:
    await tasks.finish(session, task.id, status=status, error_code=code)
    await session.commit()


async def test_due_items_are_the_ones_whose_time_has_come(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_now")
    await boxed(db_session, bot, "om_batch", level="batch", hold_until=now + timedelta(minutes=5))
    await boxed(db_session, bot, "om_held", level="hold", hold_until=now - timedelta(seconds=1))
    due = await delivery.due_items(db_session, bot.id, now)
    assert [row.message_id for row in due] == ["om_now", "om_held"]
    assert SECRET in due[0].summary_enc["text"]
    later = await delivery.due_items(db_session, bot.id, now + timedelta(minutes=5))
    assert [row.message_id for row in later] == ["om_now", "om_batch", "om_held"]


async def test_dispatch_queues_one_turn_and_binds_its_items(db_session: AsyncSession) -> None:
    bot, owner = await ready(db_session)
    first = await boxed(db_session, bot, "om_1")
    second = await boxed(db_session, bot, "om_2")
    task = await send(db_session, bot)
    assert task is not None and task.kind == HANDOVER_KIND
    assert (task.session_key, task.user_id, task.inbound_event_id) == ("oc_private", owner.id, None)
    # 载荷里只有条目编号与平台自己写的三个键。
    assert task.payload["items"] == [first.id, second.id]
    assert set(task.payload) == {"items", "serialize_session", "bot_key", "platform_user_id"}
    stored = await raw(db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id)
    assert SECRET not in stored and "om_1" not in stored
    rows = await inbox_rows(db_session, bot)
    assert [(row.status, row.delivered_task_id) for row in rows] == [("pending", task.id)] * 2
    # 装进一轮的条目不再算「到点待交回」。
    assert await delivery.due_items(db_session, bot.id, utcnow()) == []


async def test_at_most_one_turn_at_a_time(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    task = await send(db_session, bot)
    assert task is not None
    # 上一轮还没结束：新条目不另开任务。
    await boxed(db_session, bot, "om_2")
    assert await send(db_session, bot) is None
    await db_session.execute(update(Task).where(Task.id == task.id).values(status="running"))
    await db_session.commit()
    assert await send(db_session, bot) is None
    assert await handovers(db_session, bot) == [task.id]
    # 任务行结束了但还没记账：先对账才有下一轮。
    await end(db_session, task, "succeeded")
    assert await send(db_session, bot) is None
    assert await delivery.reconcile(db_session) == 1
    await db_session.commit()
    again = await send(db_session, bot)
    assert again is not None and again.id != task.id
    assert [row.message_id for row in await delivery.bound(db_session, again)] == ["om_2"]


async def test_two_schedulers_cannot_both_queue_a_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    factory = make_session_factory(db_engine)
    now = utcnow()

    async def one() -> Task | None:
        async with factory() as session:
            mine = await session.get(Bot, bot.id)
            assert mine is not None
            task = await delivery.dispatch(session, mine, now)
            await session.commit()
            return task

    results = await asyncio.gather(*(one() for _ in range(4)))
    assert len([task for task in results if task is not None]) == 1
    assert len(await handovers(db_session, bot)) == 1


async def test_batched_and_held_items_wait_for_their_time(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_batch", level="batch", hold_until=now + timedelta(minutes=5))
    await boxed(db_session, bot, "om_held", level="hold", hold_until=now + timedelta(minutes=10))
    assert await send(db_session, bot, now) is None
    assert await send(db_session, bot, now + timedelta(minutes=4, seconds=59)) is None
    task = await send(db_session, bot, now + timedelta(minutes=5))
    assert task is not None
    assert [row.message_id for row in await delivery.bound(db_session, task)] == ["om_batch"]
    await end(db_session, task, "succeeded")
    await delivery.settle(db_session, task, True)
    await db_session.commit()
    assert await send(db_session, bot, now + timedelta(minutes=9)) is None
    assert await send(db_session, bot, now + timedelta(minutes=10)) is not None


async def test_only_a_turn_that_ends_well_marks_its_items_delivered(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    task = await send(db_session, bot)
    assert task is not None
    assert [row.status for row in await inbox_rows(db_session, bot)] == ["pending"]
    await end(db_session, task, "succeeded")
    assert await delivery.settle(db_session, task, True) == 1
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.failures) == ("delivered", task.id, 0)
    # 再记一次什么都不发生。
    assert await delivery.settle(db_session, task, True) == 0
    assert await delivery.settle(db_session, task, False) == 0


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("cancelled", "superseded"),
        ("failed", "platform_turn_yielded"),
        ("failed", "assistant_paused"),
        ("cancelled", "handover_inactive"),
    ],
)
async def test_a_turn_that_did_not_happen_puts_its_items_back_for_the_next_one(
    db_session: AsyncSession, status: str, code: str
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    task = await send(db_session, bot, now)
    assert task is not None
    await end(db_session, task, status, code)
    assert await delivery.settle(db_session, task, False) == 1
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.failures) == ("pending", None, 0)
    # 不马上重排：至少推后一分钟。
    assert row.hold_until is not None and row.hold_until > now + timedelta(seconds=30)
    assert await send(db_session, bot, now) is None
    await boxed(db_session, bot, "om_2")
    later = now + timedelta(minutes=2)
    again = await send(db_session, bot, later)
    assert again is not None and again.id != task.id
    assert [item.message_id for item in await delivery.bound(db_session, again)] == [
        "om_1",
        "om_2",
    ]


async def test_real_failures_back_off_and_give_up_after_three(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    for attempt in range(1, delivery.MAX_FAILURES + 1):
        task = await send(db_session, bot, now + timedelta(hours=attempt))
        assert task is not None
        await end(db_session, task, "failed", "relay_error")
        await delivery.settle(db_session, task, False)
        await db_session.commit()
        (row,) = await inbox_rows(db_session, bot)
        assert row.failures == attempt and row.delivered_task_id is None
        if attempt < delivery.MAX_FAILURES:
            assert row.status == "pending"
            assert row.hold_until is not None and row.hold_until > utcnow() + timedelta(minutes=4)
    assert (row.status, row.void_code) == ("void", "undeliverable")
    assert await send(db_session, bot, now + timedelta(days=1)) is None


async def test_reconcile_settles_turns_that_never_reached_the_end(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    task = await send(db_session, bot, now)
    assert task is not None
    # 还在排队：不动。
    assert await delivery.reconcile(db_session) == 0
    await end(db_session, task, "cancelled", "superseded")
    assert await delivery.reconcile(db_session) == 1
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id) == ("pending", None)
    # 任务行被清理掉了也一样放回。
    task = await send(db_session, bot, now + timedelta(minutes=2))
    assert task is not None
    await db_session.execute(Task.__table__.delete().where(Task.id == task.id))
    await db_session.commit()
    assert await delivery.reconcile(db_session) == 1
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.failures) == ("pending", None, 0)


async def test_when_the_budget_is_spent_only_urgent_items_go_and_the_owner_is_told_once(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    (await settings.get(db_session, bot.id)).daily_turn_budget = 10
    await db_session.commit()
    now = utcnow()
    await spend(db_session, bot, 10, now)
    past = now - timedelta(minutes=1)
    await boxed(db_session, bot, "om_urgent")
    await boxed(db_session, bot, "om_batch", level="batch", hold_until=past)
    await boxed(db_session, bot, "om_held", level="hold", hold_until=past)
    task = await send(db_session, bot, now)
    assert task is not None
    assert [row.message_id for row in await delivery.bound(db_session, task)] == ["om_urgent"]
    assert await raw(db_session, NOTICES, bot=bot.id) == 1
    notice = await raw(
        db_session,
        "SELECT payload::text || target::text FROM outbox"
        " WHERE bot_id = :bot AND dedupe_key LIKE 'assistant-budget:%'",
        bot=bot.id,
    )
    assert SECRET not in notice and "oc_private" in notice
    # 之后每 5 秒再来一次也不重复私信；攒批与压着的一直留在箱里。
    await delivery.settle(db_session, task, True)
    await db_session.commit()
    for _ in range(3):
        assert await send(db_session, bot, now) is None
    assert await raw(db_session, NOTICES, bot=bot.id) == 1
    waiting = [
        row.message_id for row in await inbox_rows(db_session, bot) if row.status == "pending"
    ]
    assert waiting == ["om_batch", "om_held"]


async def test_urgent_items_stop_too_once_the_overdraft_is_used_up(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    (await settings.get(db_session, bot.id)).daily_turn_budget = 10
    await db_session.commit()
    now = utcnow()
    await spend(db_session, bot, 10 + delivery.OVERDRAFT, now)
    await boxed(db_session, bot, "om_urgent")
    assert await send(db_session, bot, now) is None
    assert await raw(db_session, NOTICES, bot=bot.id) == 1
    assert await send(db_session, bot, now) is None
    assert await raw(db_session, NOTICES, bot=bot.id) == 1


async def test_the_next_day_everything_goes_again(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    (await settings.get(db_session, bot.id)).daily_turn_budget = 10
    await db_session.commit()
    now = utcnow()
    await spend(db_session, bot, 10, now)
    await boxed(db_session, bot, "om_batch", level="batch", hold_until=now - timedelta(minutes=1))
    assert await send(db_session, bot, now) is None
    task = await send(db_session, bot, now + timedelta(days=1))
    assert task is not None
    assert [row.message_id for row in await delivery.bound(db_session, task)] == ["om_batch"]


async def test_nothing_is_queued_while_paused_and_everything_goes_on_resume(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    relay.assistant_pool = False
    await db_session.commit()
    outgoing = "SELECT count(*) FROM outbox WHERE bot_id = :bot"
    before = await raw(db_session, outgoing, bot=bot.id)
    await boxed(db_session, bot, "om_1")
    await boxed(db_session, bot, "om_2")
    for _ in range(3):
        assert await send(db_session, bot) is None
    # 条目原样留着，没有排任务，也没有任何私信。
    assert [row.delivered_task_id for row in await inbox_rows(db_session, bot)] == [None, None]
    assert await handovers(db_session, bot) == []
    assert await raw(db_session, outgoing, bot=bot.id) == before
    relay.assistant_pool = True
    await db_session.commit()
    task = await send(db_session, bot)
    assert task is not None
    assert len(await delivery.bound(db_session, task)) == 2


@pytest.mark.parametrize("change", ["off_shift", "cold_start", "deprovisioning", "disabled"])
async def test_the_gate_keeps_items_in_the_box(db_session: AsyncSession, change: str) -> None:
    bot, _ = await ready(db_session)
    prefs = await settings.get(db_session, bot.id)
    if change == "off_shift":
        prefs.shift_override = "off"
    elif change == "cold_start":
        prefs.cold_start_done_at = None
    elif change == "deprovisioning":
        bot.assistant_state = "deprovisioning"
    else:
        bot.enabled = False
    await db_session.commit()
    await boxed(db_session, bot, "om_1")
    assert await send(db_session, bot) is None
    assert await handovers(db_session, bot) == []
    assert (await inbox_rows(db_session, bot))[0].delivered_task_id is None


async def test_settling_another_task_does_not_touch_this_turns_items(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    db_session.add(AssistantInbox(bot_id=bot.id, level="immediate", message_id="om_1"))
    await db_session.commit()
    task = await send(db_session, bot)
    assert task is not None
    other = Task(id=task.id + 1000, bot_id=bot.id, kind="chat")
    assert await delivery.settle(db_session, other, True) == 0
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id) == ("pending", task.id)
