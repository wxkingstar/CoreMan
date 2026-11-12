"""所有者在私聊里停用助理：「停用助理」→ 确认卡片 → 只有所有者带着平台签的未过期令牌才能确认。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import runtime
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, Task, User
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.lifecycle_helpers import active_assistant, flow_of, install
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import build_ctx, seed_bot

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def stop_card(
    db_engine: AsyncEngine, db_session: AsyncSession, bot: Bot
) -> tuple[Task, FakeRelay, OutboxItem]:
    """所有者在私聊里发「停用助理」，这一轮跑完：返回任务、模型替身与发出去的确认卡片。"""
    task = await chat_task(
        db_session, bot, "停用助理", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    task.status = "running"
    await db_session.commit()
    relay = FakeRelay("normal")
    await ChatTaskHandler().run(
        build_ctx(db_engine, task, relay_client_factory=lambda _r: relay.client())
    )
    sent = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
    )
    assert sent is not None
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay, sent


async def click(
    db_session: AsyncSession,
    bot: Bot,
    sent: OutboxItem,
    *,
    who: str = "owner_pid",
    chat: str = "oc_private",
    **edits: Any,
) -> Task:
    """点了确认卡片上的按钮：卡片已发到私聊（出站记录 sent），回调按网关的样子落库并排成任务。"""
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": "om_stop_card"}
    value = sent.payload["card"]["body"]["elements"][1]["behaviors"][0]["value"]
    action = {**value, "card_type": "form", "selected": {}, **edits}
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4().hex}",
        kind="card_action",
        chat_type="single",
        chat_id=chat,
        sender_platform_user_id=who,
        sender_open_id=f"ou_{who}",
        reply_context={"chat_id": chat, "message_id": "om_stop_card"},
        payload={},
    )
    db_session.add(event)
    await db_session.flush()
    task = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": who},
            session_key=chat,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await db_session.commit()
    return task


async def test_the_owner_confirms_and_the_assistant_starts_deprovisioning(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    done, relay, sent = await stop_card(db_engine, db_session, bot)
    # 命令不经模型：这一轮没有派给节点，卡片发到了私聊。
    assert done.status == "succeeded" and relay.requests == []
    assert sent.payload["card"]["task_id"] == f"personal:{done.id}"
    assert sent.target == {"chat_id": "oc_private"}
    task = await click(db_session, bot, sent)
    await CardActionHandler().run(build_ctx(db_engine, task))
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning" and bot.enabled
    assert await runtime.turn_blocker(db_session, bot) == "deprovisioning"
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.trigger == "owner"
    update = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "card_update")
    )
    assert update is not None and "正在停用" in str(update.payload)


async def test_an_expired_card_only_says_so(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from coreman.runtime.worker import assistant_stop

    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    real = time.time()
    monkeypatch.setattr(assistant_stop.time, "time", lambda: real - assistant_stop.TTL_SECONDS - 5)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    monkeypatch.setattr(assistant_stop.time, "time", lambda: real)
    task = await click(db_session, bot, sent)
    await CardActionHandler().run(build_ctx(db_engine, task))
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    assert await flow_of(db_session, owner.id) is None


async def test_only_the_owner_with_a_fresh_signed_card_can_stop_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    stranger = User(login_name="lisi", display_name="李四")
    db_session.add(stranger)
    await db_session.commit()
    expires, _, mac = sent.payload["card"]["body"]["elements"][1]["behaviors"][0]["value"][
        "level"
    ].partition(".")
    for edits, who in (
        ({}, "lisi"),  # 不是所有者
        ({"level": f"{expires}.{'0' * 64}"}, "owner_pid"),  # 令牌不是平台签的
        ({"level": f"{int(expires) + 60}.{mac}"}, "owner_pid"),  # 改了到期时刻
        ({"event_key": "personal_schedule"}, "owner_pid"),  # 别的卡片
    ):
        task = await click(db_session, bot, sent, who=who, **edits)
        await CardActionHandler().run(build_ctx(db_engine, task))
        bot = await db_session.get(Bot, bot.id, populate_existing=True)
        assert bot is not None and bot.assistant_state == "active"
    assert await flow_of(db_session, owner.id) is None


async def test_stop_is_not_a_command_for_ordinary_bots(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """普通员工收到「停用助理」就是一句普通的话：照常派给模型。"""
    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "停用助理")
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert len(relay.requests) == 1
    assert (await tasks.get(db_session, task.id)).status == "succeeded"  # type: ignore[union-attr]
