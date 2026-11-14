"""交回的一轮在 worker 里：文本是整理稿；正常结束才记已交回；被打断的下一轮再带上。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import delivery, settings
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, Task
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.assistant_handover import AssistantHandoverHandler
from coreman.runtime.worker.chat.models import Verdict
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import boxed, inbox_rows, ready
from tests.integration.test_assistant_platform_turn_handler import run, streams_of
from tests.integration.test_assistant_platform_turn_yield import fresh, owner_message
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


async def handed(session: AsyncSession, bot: Bot, now: Any = None) -> Task:
    """排一轮交回并认领它。"""
    task = await delivery.dispatch(session, bot, now or utcnow())
    await session.commit()
    assert task is not None
    claimed = await claim(session)
    assert claimed.id == task.id
    return claimed


async def left_today(session: AsyncSession, bot: Bot) -> int:
    prefs = await settings.get(session, bot.id)
    return await delivery.budget_left(session, bot, settings.local_today(prefs, utcnow()))


async def test_the_turn_gets_the_digest_and_settles_when_it_ends_well(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    await boxed(db_session, bot, "om_2", level="batch", chat="oc_group", text="下周例会改期")
    before = await left_today(db_session, bot)
    task = await handed(db_session, bot)
    done, relay = await run(db_engine, db_session, task, AssistantHandoverHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert text.startswith("以下是平台交回的 2 件事")
    assert "定位：chat_id=oc_li message_id=om_1" in text
    assert f"> 周五能给报价吗？{SECRET}" in text and "> 下周例会改期" in text
    # 和所有者私聊的一轮一样拿到个人工具；规则在稳定段里。
    assert "COREMAN_FEISHU_PERSONAL_TOKEN" in relay.requests[0]["env_vars"]
    assert "## 交回事项" in relay.requests[0]["messages"][0]["content"]
    rows = await inbox_rows(db_session, bot)
    assert [(row.status, row.delivered_task_id) for row in rows] == [("delivered", task.id)] * 2
    kind = await raw(
        db_session, "SELECT message_type FROM chat_logs WHERE task_id = :id", id=task.id
    )
    assert kind == HANDOVER_KIND
    # 所有者不在场：这一轮不出声，回复不发到私聊（要他知道的记成待汇报）。
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private" and stream.reply_context["_silent"]
    # 正常开了一轮：预算少一轮。
    assert await left_today(db_session, bot) == before - 1


async def test_nothing_readable_leaves_the_task_or_the_logs(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    with capture_logs() as logs:
        task = await handed(db_session, bot)
        await run(db_engine, db_session, task, AssistantHandoverHandler())
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    assert SECRET not in dumped and "周五能给报价吗" not in dumped
    stored = await raw(
        db_session,
        "SELECT payload::text || coalesce(result::text, '') FROM tasks WHERE id = :id",
        id=task.id,
    )
    assert SECRET not in stored and "周五能给报价吗" not in stored


async def test_a_turn_cancelled_by_the_owners_message_hands_its_items_to_the_next_one(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    turn = await handed(db_session, bot, now)
    # 所有者发来一条：chat 轮次把在跑的平台轮次标成被替代。
    message = await owner_message(db_session, bot)
    handler = ChatTaskHandler()
    handler.SUPERSEDE_WAIT_SECONDS = 0.3
    chat_relay = FakeRelay("normal")
    ctx = build_ctx(db_engine, message, relay_client_factory=lambda _r: chat_relay.client())
    await handler.run(ctx)
    assert (await fresh(db_session, turn)).cancel_reason == "superseded"
    done, relay = await run(
        db_engine, db_session, await fresh(db_session, turn), AssistantHandoverHandler()
    )
    assert done.status == "cancelled" and relay.requests == []
    # 没跑到收尾：靠对账把条目放回待交回。
    assert await delivery.reconcile(db_session) == 1
    await db_session.commit()
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.failures) == ("pending", None, 0)
    # 所有者那条做完之后的下一轮：带上放回的这条和期间新到的一条。
    await tasks.finish(db_session, message.id, status="succeeded")
    await db_session.commit()
    await boxed(db_session, bot, "om_2", text="第二件事")
    again = await handed(db_session, bot, now + timedelta(minutes=2))
    done, relay = await run(db_engine, db_session, again, AssistantHandoverHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert "message_id=om_1" in text and "message_id=om_2" in text
    rows = await inbox_rows(db_session, bot)
    assert [(row.status, row.delivered_task_id) for row in rows] == [("delivered", again.id)] * 2


async def test_giving_way_changes_nothing(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    """让路放回队列的那次认领也会渲染文本：不能因此改条目，也不算一轮预算。"""
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    before = await left_today(db_session, bot)
    turn = await handed(db_session, bot)
    await owner_message(db_session, bot)
    done, relay = await run(db_engine, db_session, turn, AssistantHandoverHandler())
    assert done.status == "queued" and relay.requests == []
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.failures) == ("pending", turn.id, 0)
    assert await delivery.reconcile(db_session) == 0
    assert await left_today(db_session, bot) == before


@pytest.mark.parametrize(
    ("status", "delivered"), [("succeeded", True), ("cancelled", False), ("failed", False)]
)
async def test_the_closing_hook_settles_by_how_the_turn_ended(
    db_engine: AsyncEngine, db_session: AsyncSession, status: str, delivered: bool
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    turn = await handed(db_session, bot)
    await tasks.finish(
        db_session, turn.id, status=status, error_code=None if delivered else "superseded"
    )
    await db_session.commit()
    ctx = build_ctx(db_engine, turn)
    verdict = Verdict("success" if delivered else "stopped", status, None, None, "")
    await AssistantHandoverHandler()._after_finalize(ctx, cast(Any, None), verdict)
    (row,) = await inbox_rows(db_session, bot)
    assert row.status == ("delivered" if delivered else "pending")
    assert row.delivered_task_id == (turn.id if delivered else None)


async def test_a_turn_with_nothing_left_to_hand_over_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    turn = await handed(db_session, bot)
    await delivery.settle(db_session, turn, True)
    await db_session.commit()
    done, relay = await run(db_engine, db_session, turn, AssistantHandoverHandler())
    assert done.status == "cancelled" and done.error_code == "handover_inactive"
    assert relay.requests == [] and await streams_of(db_session, turn) == []


async def test_items_from_a_turn_that_started_and_did_not_finish_are_marked_next_time(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """开了流的一轮没走完（被打断、中途失败）：模型可能已经处理了一部分，而下一轮不一定还记得
    （被打断那一轮的转录不保证续得上）。重交回时在这些条目上写明，让它先核对。"""
    bot, _ = await ready(db_session)
    now = utcnow()
    await boxed(db_session, bot, "om_1")
    turn = await handed(db_session, bot, now)
    relay = FakeRelay("error_finish")
    ctx = build_ctx(db_engine, turn, relay_client_factory=lambda _r: relay.client())
    await AssistantHandoverHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert "交回过" not in user_input(relay.requests[0])
    (row,) = await inbox_rows(db_session, bot)
    assert (row.status, row.delivered_task_id, row.handovers) == ("pending", None, 1)
    await boxed(db_session, bot, "om_2", text="第二件事")
    again = await handed(db_session, bot, now + timedelta(hours=1))
    done, relay = await run(db_engine, db_session, again, AssistantHandoverHandler())
    assert done.status == "succeeded"
    first, second = user_input(relay.requests[0]).split("【2】")
    assert "message_id=om_1" in first and "这件事之前交回过 1 次" in first
    assert "message_id=om_2" in second and "交回过" not in second
    assert [row.handovers for row in await inbox_rows(db_session, bot)] == [2, 1]


async def test_a_turn_that_never_started_leaves_no_mark(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await ready(db_session)
    await boxed(db_session, bot, "om_1")
    turn = await handed(db_session, bot)
    # 让路：渲染过文本，但没开流。
    await owner_message(db_session, bot)
    done, relay = await run(db_engine, db_session, turn, AssistantHandoverHandler())
    assert done.status == "queued" and relay.requests == []
    assert [row.handovers for row in await inbox_rows(db_session, bot)] == [0]
