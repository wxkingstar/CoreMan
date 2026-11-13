"""回复按钮的文字是助理生成的内容：落库时进密文，路由用的键留明文。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys
from coreman.core.bus import streams
from coreman.core.chat.card_replies import REPLY_BUTTON, clicked, enqueue_used
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, Task, TaskStream
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_cards.reply_buttons import UsedRow, reply_value
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.worker.replies import load_inbound
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
APP = "cli_example"
ROUTING = {"chat_id": "oc_1", "chat_type": "group", "message_id": "om_card"}


def _click(bot: Bot) -> Any:
    event = {
        "header": {"app_id": APP, "event_type": "card.action.trigger", "event_id": "ev_click"},
        "event": {
            "operator": {"user_id": "u_zhang", "open_id": "ou_zhang"},
            "context": {"open_chat_id": "oc_1", "open_message_id": "om_card"},
            "action": {
                "value": reply_value(SECRET, requester="u_zhang", row="row_1", buttons=["btn_1"])
            },
        },
    }
    message = normalize_event(
        event,
        bot_id=bot.id,
        app_id=APP,
        bot_open_id="",
        gateway_instance="gw-a",
        now=datetime.now(UTC),
    )
    assert message is not None and clicked(message.reply_context) is not None
    return message


def _snapshot(bot: Bot) -> SimpleNamespace:
    return SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)


async def _land(session: AsyncSession, bot: Bot) -> Task:
    task = await enqueue_inbound(session, _snapshot(bot), _click(bot), lease_generation=0)
    await session.commit()
    assert task is not None and task.inbound_event_id is not None
    return task


async def _stored_context(session: AsyncSession, table: str, column: str, value: int) -> Any:
    return await raw(
        session,
        f"SELECT reply_context FROM {table} WHERE {column} = :v",  # noqa: S608
        v=value,
    )


async def test_button_text_is_sealed_and_routing_keys_stay_plain(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task = await _land(db_session, bot)
    stored = await _stored_context(db_session, "inbound_events", "id", task.inbound_event_id)
    assert SECRET not in str(stored) and REPLY_BUTTON not in stored
    assert stored["_sealed"].startswith("enc:v2:d1:")
    assert {key: stored[key] for key in ROUTING} == ROUTING
    assert stored["gateway_instance"] == "gw-a" and "received_at" in stored
    factory = make_session_factory(db_engine)
    async with factory() as other:
        # 取整行自动还原：worker 读到按钮文字，照常排置灰。
        event = await load_inbound(other, task)
        meta = clicked(event.reply_context)
        assert meta is not None and meta["label"] == SECRET and meta["buttons"] == ["btn_1"]
        assert {key: event.reply_context[key] for key in ROUTING} == ROUTING
        await enqueue_used(other, bot.id, event)
        # 这一轮的流沿用入站的回复上下文：流上只留路由用的键。
        await streams.create(
            other,
            task_id=task.id,
            bot_id=bot.id,
            platform="feishu",
            stream_id="s1",
            reply_context={**event.reply_context, "requester_user_id": "u_zhang", "_observe": True},
            lease_generation=1,
            running_since=datetime.now(UTC),
        )
        await other.commit()
    on_stream = await _stored_context(db_session, "task_streams", "task_id", task.id)
    assert SECRET not in str(on_stream) and REPLY_BUTTON not in on_stream
    assert on_stream == {
        **ROUTING,
        "gateway_instance": "gw-a",
        "received_at": stored["received_at"],
        "requester_user_id": "u_zhang",
        "_observe": True,
    }
    # 置灰的出站：按钮文字同样在密文里，出站循环取出时读得到。
    queued = str(
        await raw(db_session, "SELECT payload::text FROM outbox WHERE bot_id = :b", b=bot.id)
    )
    assert SECRET not in queued and "_reply_used" not in queued
    async with factory() as other:
        item = await other.scalar(select(OutboxItem).where(OutboxItem.bot_id == bot.id))
        used = UsedRow.parse(item.payload["_reply_used"])
        assert used is not None and used.label == SECRET
        assert item.payload["_operator"] == "u_zhang"
    # 密钥销毁之后：读不出按钮文字，不抛错，路由用的键还在。
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    async with factory() as other:
        event = await load_inbound(other, task)
        assert clicked(event.reply_context) is None
        assert {key: event.reply_context[key] for key in ROUTING} == ROUTING
        await enqueue_used(other, bot.id, event)
        rows = (
            await other.scalars(select(InboundEvent).where(InboundEvent.bot_id == bot.id))
        ).all()
        # 没经过还原的读取处（取多行）：路由用的键就在封装顶层。
        assert rows[0].reply_context["chat_id"] == "oc_1"


async def test_standard_bot_reply_context_is_unchanged(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    message = _click(bot)
    task = await _land(db_session, bot)
    stored = await _stored_context(db_session, "inbound_events", "id", task.inbound_event_id)
    assert stored == message.reply_context | {"received_at": stored["received_at"]}
    assert stored[REPLY_BUTTON]["label"] == SECRET and "_sealed" not in stored
    event = await load_inbound(db_session, task)
    await enqueue_used(db_session, bot.id, event)
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context=event.reply_context,
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await db_session.commit()
    assert await _stored_context(db_session, "task_streams", "task_id", task.id) == stored
    queued = await raw(db_session, "SELECT payload FROM outbox WHERE bot_id = :b", b=bot.id)
    assert queued == {
        "_reply_used": {
            "row": "row_1",
            "buttons": ["btn_1"],
            "label": SECRET,
            "chat_type": "group",
        },
        "_operator": "u_zhang",
    }
    assert (await db_session.scalars(select(TaskStream.bot_id))).all() == [bot.id]
