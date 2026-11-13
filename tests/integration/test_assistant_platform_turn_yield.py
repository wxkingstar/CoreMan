"""平台来源的一轮开轮时不取消所有者的消息：同会话有别的任务就自己放回队列；所有者优先不变。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, Task
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.platform_turn import PlatformTurnHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_assistant_platform_turn_handler import (
    Handover,
    assistant,
    queued,
    run,
    streams_of,
)
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")


async def owner_message(session: AsyncSession, bot: Bot, **payload: Any) -> Task:
    """所有者在私聊里发来的一条，已被认领。"""
    task = await chat_task(
        session, bot, "帮我查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    if payload:
        task.payload = {**task.payload, **payload}
        await session.commit()
    return task


async def fresh(session: AsyncSession, task: Task) -> Task:
    found = await session.get(Task, task.id, populate_existing=True)
    assert found is not None
    return found


async def seconds_until_claimable(session: AsyncSession, task: Task) -> float:
    return float(
        await session.scalar(
            text("SELECT extract(epoch FROM run_after - now()) FROM tasks WHERE id = :id"),
            {"id": task.id},
        )
    )


@pytest.mark.parametrize("state", ["running", "queued"])
async def test_the_turn_goes_back_to_the_queue_behind_the_owners_message(
    db_engine: AsyncEngine, db_session: AsyncSession, state: str
) -> None:
    bot, _ = await assistant(db_session)
    turn = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    message = await owner_message(db_session, bot)
    if state == "queued":
        await db_session.execute(
            update(Task).where(Task.id == message.id).values(status="queued", claimed_by=None)
        )
        await db_session.commit()
    done, relay = await run(db_engine, db_session, turn, Handover())
    assert done.status == "queued" and done.cancel_requested_at is None
    assert relay.requests == [] and await streams_of(db_session, turn) == []
    # 退避：不是马上又能被认领。
    assert await seconds_until_claimable(db_session, turn) > 5
    kept = await fresh(db_session, message)
    assert kept.cancel_requested_at is None and kept.status == state.replace("running", "claimed")


async def test_each_retry_waits_longer(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    turn = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    await owner_message(db_session, bot)
    await run(db_engine, db_session, turn, Handover())
    first = await seconds_until_claimable(db_session, turn)
    await db_session.execute(
        update(Task).where(Task.id == turn.id).values(status="claimed", attempts=4)
    )
    await db_session.commit()
    await run(db_engine, db_session, await fresh(db_session, turn), Handover())
    assert await seconds_until_claimable(db_session, turn) > first * 2


async def test_it_gives_up_quietly_after_too_many_retries(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    turn = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    message = await owner_message(db_session, bot)
    await db_session.execute(
        update(Task)
        .where(Task.id == turn.id)
        .values(attempts=PlatformTurnHandler.YIELD_MAX_ATTEMPTS)
    )
    await db_session.commit()
    done, relay = await run(db_engine, db_session, await fresh(db_session, turn), Handover())
    assert done.status == "failed" and done.error_code == "platform_turn_yielded"
    assert relay.requests == [] and await streams_of(db_session, turn) == []
    assert (await fresh(db_session, message)).cancel_requested_at is None


async def test_a_message_queued_behind_the_turn_does_not_hold_it_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """排在这一轮后面等它做完的消息（serialize_session）认领不到：让给它就两边互等。"""
    bot, _ = await assistant(db_session)
    turn = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    message = await owner_message(db_session, bot, serialize_session=True)
    await db_session.execute(
        update(Task).where(Task.id == message.id).values(status="queued", claimed_by=None)
    )
    await db_session.commit()
    done, relay = await run(db_engine, db_session, turn, Handover())
    assert done.status == "succeeded" and len(relay.requests) == 1


async def test_the_owners_message_still_cancels_a_running_platform_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """所有者优先：他一发消息，正在跑的平台轮次被取消（事项回到待交回，下次再带上）。"""
    bot, _ = await assistant(db_session)
    turn = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    message = await owner_message(db_session, bot)
    handler = ChatTaskHandler()
    # 没有人在跑那一轮平台任务，它不会退出：等一小会儿就放回队列，只看取消标记。
    handler.SUPERSEDE_WAIT_SECONDS = 0.3
    relay = FakeRelay("normal")
    ctx = build_ctx(db_engine, message, relay_client_factory=lambda _r: relay.client())
    await handler.run(ctx)
    cancelled = await fresh(db_session, turn)
    assert cancelled.cancel_requested_at is not None and cancelled.cancel_reason == "superseded"
    assert (await tasks.heartbeat(db_session, turn.id))[0] is tasks.Heartbeat.CANCELLED
