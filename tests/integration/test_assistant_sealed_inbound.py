"""inbound_events：事件载荷加密入库，路由信息与去重键不变。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from coreman.core.wecom.messages import InboundMessage, Sender, TextPart
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.replies import load_inbound
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")


def _message(bot: Bot) -> InboundMessage:
    return InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="single",
        chat_id="oc_1",
        sender=Sender(platform_user_id="ou_1"),
        message_id="om_1",
        parts=[TextPart(text=SECRET)],
        reply_context={"chat_id": "oc_1", "message_id": "om_1"},
        raw={"event": {"message": {"message_type": "text", "content": SECRET}}},
    )


def _snapshot(bot: Bot) -> SimpleNamespace:
    return SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)


async def test_event_payload_is_sealed_and_readable(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    await db_session.commit()
    assert task is not None and task.inbound_event_id is not None
    event = await raw(
        db_session,
        "SELECT payload::text || '|' || reply_context::text FROM inbound_events WHERE id = :id",
        id=task.inbound_event_id,
    )
    assert SECRET not in event and "_sealed" in event and '"message_id": "om_1"' in event
    stored_task = await raw(
        db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id
    )
    assert SECRET not in stored_task
    async with make_session_factory(db_engine)() as other:
        loaded = await load_inbound(other, task)
        assert loaded.payload["parts"][0]["text"] == SECRET
        assert loaded.payload["raw"]["event"]["message"]["content"] == SECRET
    # 平台重推：去重键不受加密影响。
    again = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    assert again is None


async def test_standard_bot_event_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    await db_session.commit()
    assert task is not None
    event = await raw(
        db_session,
        "SELECT payload::text FROM inbound_events WHERE id = :id",
        id=task.inbound_event_id,
    )
    assert SECRET in event and "_sealed" not in event


async def test_destroyed_key_drops_the_event_payload(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    # 销毁之后到达的消息：事件与任务照常落库，内容丢弃，路由信息还在。
    task = await enqueue_inbound(db_session, _snapshot(bot), _message(bot), lease_generation=0)
    await db_session.commit()
    assert task is not None and task.inbound_event_id is not None
    event = await raw(
        db_session,
        "SELECT payload::text || '|' || reply_context::text FROM inbound_events WHERE id = :id",
        id=task.inbound_event_id,
    )
    assert SECRET not in event and event.startswith('{"_sealed": ""}|')
    assert '"message_id": "om_1"' in event
    stored_task = await raw(
        db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id
    )
    assert SECRET not in stored_task and '"_sealed": ""' in stored_task
    loaded = await load_inbound(db_session, task)
    assert sealed.is_locked(loaded) and loaded.payload == {}
