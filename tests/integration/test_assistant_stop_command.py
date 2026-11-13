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


# ---- 评审补充：先于运行时与待答截下、只截私聊、别人发的、重复确认、合并 ------------------------


async def _sent_stop_cards(session: AsyncSession, bot: Bot) -> list[OutboxItem]:
    rows = await session.scalars(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
    )
    return [row for row in rows if "停用个人助理" in str(row.payload)]


async def test_the_command_works_while_the_runtime_is_unavailable(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.db.models import RelayServer

    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    relay_row.is_active = False
    await db_session.commit()
    done, relay, sent = await stop_card(db_engine, db_session, bot)
    assert done.status == "succeeded" and done.result == {"assistant": "stop_offered"}
    assert relay.requests == [] and "停用个人助理" in str(sent.payload)


async def test_the_command_is_not_taken_as_the_answer_to_a_pending_question(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from coreman.core.chat import interactions
    from tests.integration.test_card_actions import Q1, _state

    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    st = await _state(db_session, bot, questions=[Q1], user="owner_pid")
    await interactions.patch_state(
        db_session,
        st.id,
        {"waiting_for_text": True, "waiting_since": datetime.now(UTC).isoformat()},
        bot_id=bot.id,
    )
    await db_session.commit()
    done, relay, sent = await stop_card(db_engine, db_session, bot)
    assert done.result == {"assistant": "stop_offered"} and relay.requests == []
    again = await interactions.get_open(
        db_session,
        kind=interactions.KIND_CHOICE,
        scope_key=interactions.choice_scope(bot.id, "owner_pid"),
    )
    assert again is not None and again.state["answers"] == []
    assert again.state["waiting_for_text"] is True


async def test_the_command_in_a_group_is_not_intercepted(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    task = await chat_task(
        db_session, bot, "停用助理", sender="owner_pid", chat_type="group", chat_id="oc_group"
    )
    await run(db_engine, task, FakeRelay("normal"))
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None and (done.result or {}).get("assistant") is None
    assert await _sent_stop_cards(db_session, bot) == []


async def test_someone_else_sending_the_command_gets_no_card(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    task = await chat_task(
        db_session, bot, "停用助理", sender="lisi", chat_type="single", chat_id="oc_lisi"
    )
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.status == "succeeded"
    assert done.result == {"assistant": "stop_not_owner"} and relay.requests == []
    assert await _sent_stop_cards(db_session, bot) == []


async def test_a_non_ascii_token_is_ignored_without_crashing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    expires = sent.payload["card"]["body"]["elements"][1]["behaviors"][0]["value"]["level"]
    task = await click(db_session, bot, sent, level=f"{expires.partition('.')[0]}.停用")
    await CardActionHandler().run(build_ctx(db_engine, task))
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.status == "succeeded"
    assert done.result == {"card": "ignored"}
    assert await flow_of(db_session, owner.id) is None


async def test_confirming_an_already_stopped_assistant_does_not_say_stopping(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    _, _, sent = await stop_card(db_engine, db_session, bot)
    bot.assistant_state = "deprovisioned"
    await db_session.commit()
    task = await click(db_session, bot, sent)
    await CardActionHandler().run(build_ctx(db_engine, task))
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.result == {"card": "assistant_already_stopped"}
    update = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "card_update")
    )
    assert update is not None and "正在停用" not in str(update.payload)


async def test_a_queued_stop_command_is_not_merged_into_the_running_turn(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, _ = await active_assistant(db_session)
    kw: dict[str, Any] = {"sender": "owner_pid", "chat_type": "single", "chat_id": "oc_private"}
    head = await chat_task(db_session, bot, "帮我看看今天的消息", **kw)
    head.status = "running"
    await db_session.commit()
    follow = await chat_task(db_session, bot, "顺便看下日程", **kw)
    follow.status = "running"
    await db_session.commit()
    stop = await chat_task(db_session, bot, "停用助理", **kw)
    for row in (follow, stop):
        row.status, row.claimed_by = "queued", None
    await db_session.commit()
    relay = FakeRelay("normal")
    await run(db_engine, head, relay)
    merged = await db_session.get(Task, follow.id, populate_existing=True)
    assert merged is not None and merged.error_code == "merged"
    waiting = await db_session.get(Task, stop.id, populate_existing=True)
    assert waiting is not None and waiting.status == "queued" and waiting.error_code is None
    assert "停用助理" not in str(relay.requests)
