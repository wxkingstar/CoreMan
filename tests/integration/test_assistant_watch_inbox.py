"""收件箱的写入：摘要是密文、同一条消息只入一次、压着只往后推、作废不碰正在交回的条目。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantInbox
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
OWNER = "ou_owner"


def item(mid: str = "om_1", **over: Any) -> Item:
    values: dict[str, Any] = {
        "chat_id": "oc_li",
        "chat_kind": "p2p",
        "message_id": mid,
        "sender_id": "ou_li",
        "sender_type": "user",
        "at_ms": int(utcnow().timestamp() * 1000),
        "text": f"周五能给报价吗？{SECRET}",
        "chat_name": "李四",
    }
    values.update(over)
    return Item(**values)


async def owned(session: AsyncSession) -> uuid.UUID:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    return bot.id


async def rows(session: AsyncSession, bot_id: uuid.UUID) -> list[AssistantInbox]:
    found = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == bot_id)
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, found)
    return found


async def test_an_item_is_stored_with_a_sealed_summary(db_session: AsyncSession) -> None:
    bot_id = await owned(db_session)
    one = item(thread_id="omt_1", mentions=frozenset({OWNER}), text="长" * 600 + SECRET)
    assert await inbox.add(
        db_session, bot_id, one, Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
    await db_session.commit()
    stored = str(await raw(db_session, "SELECT summary_enc::text FROM assistant_inbox"))
    assert "enc:v2:d1:" in stored
    for plain in (SECRET, "长长", "李四"):
        assert plain not in stored
    [row] = await rows(db_session, bot_id)
    assert (row.kind, row.level, row.status) == ("message", "immediate", "pending")
    assert (row.chat_id, row.message_id, row.thread_id) == ("oc_li", "om_1", "omt_1")
    assert row.message_at == one.created_at and row.hold_until is None
    assert row.summary_enc == {
        "chat_kind": "p2p",
        "chat_name": "李四",
        "sender_id": "ou_li",
        "sender_type": "user",
        "msg_type": "text",
        "text": "长" * 500,
        "mentioned": True,
    }


async def test_the_same_message_is_stored_once(db_session: AsyncSession) -> None:
    bot_id, other = await owned(db_session), await owned(db_session)
    until = utcnow() + timedelta(minutes=5)
    first = await inbox.add(
        db_session, bot_id, item(), Level.BATCH, owner_open_id=OWNER, hold_until=until
    )
    again = await inbox.add(
        db_session,
        bot_id,
        item(text="改过的"),
        Level.IMMEDIATE,
        owner_open_id=OWNER,
        hold_until=None,
    )
    elsewhere = await inbox.add(
        db_session, other, item(), Level.BATCH, owner_open_id=OWNER, hold_until=until
    )
    await db_session.commit()
    assert (first, again, elsewhere) == (True, False, True)
    [row] = await rows(db_session, bot_id)
    assert row.level == "batch" and row.hold_until == until
    assert SECRET in row.summary_enc["text"]  # type: ignore[index]


async def test_batch_until_is_the_earliest_open_batch(db_session: AsyncSession) -> None:
    bot_id = await owned(db_session)
    now = utcnow()
    assert await inbox.batch_until(db_session, bot_id, now) is None
    for mid, level, minutes in (
        ("om_1", Level.BATCH, 4),
        ("om_2", Level.BATCH, 2),
        ("om_3", Level.BATCH, -1),  # 已经到点、还没被取走的一批不算
        ("om_4", Level.HOLD, 1),
    ):
        await inbox.add(
            db_session,
            bot_id,
            item(mid),
            level,
            owner_open_id=OWNER,
            hold_until=now + timedelta(minutes=minutes),
        )
    assert await inbox.batch_until(db_session, bot_id, now) == now + timedelta(minutes=2)


async def test_holding_a_chat_only_pushes_later_and_spares_other_chats(
    db_session: AsyncSession,
) -> None:
    bot_id = await owned(db_session)
    now = utcnow()
    far = now + timedelta(minutes=30)
    await inbox.add(
        db_session, bot_id, item("om_1"), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
    await inbox.add(
        db_session, bot_id, item("om_2"), Level.HOLD, owner_open_id=OWNER, hold_until=far
    )
    await inbox.add(
        db_session,
        bot_id,
        item("om_3", chat_id="oc_team", chat_kind="group"),
        Level.IMMEDIATE,
        owner_open_id=OWNER,
        hold_until=None,
    )
    until = now + timedelta(minutes=10)
    assert await inbox.hold_chat(db_session, bot_id, "oc_li", until) == 2
    await db_session.commit()
    first, second, third = await rows(db_session, bot_id)
    assert (first.level, first.hold_until) == ("hold", until)
    assert (second.level, second.hold_until) == ("hold", far)
    assert (third.level, third.hold_until) == ("immediate", None)


async def test_voiding_leaves_delivered_and_in_flight_items_alone(
    db_session: AsyncSession,
) -> None:
    bot_id = await owned(db_session)
    for mid in ("om_1", "om_2", "om_3", "om_4"):
        await inbox.add(
            db_session, bot_id, item(mid), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
        )
    await db_session.commit()
    one, two, three, four = await rows(db_session, bot_id)
    two.status = "delivered"
    three.delivered_task_id = 7  # 投递层已经把它装进一轮，正在交回
    await db_session.commit()
    assert await inbox.pending_messages(db_session, bot_id) == ["om_4", "om_1"]
    assert await inbox.pending_messages(db_session, bot_id, limit=1) == ["om_4"]
    assert await inbox.void_message(db_session, bot_id, "om_1", "recalled") == 1
    for untouched in ("om_2", "om_3", "om_x"):
        assert await inbox.void_message(db_session, bot_id, untouched, "recalled") == 0
    assert await inbox.void_all(db_session, bot_id, "stopped") == 1
    await db_session.commit()
    found = {row.message_id: (row.status, row.void_code) for row in await rows(db_session, bot_id)}
    assert found == {
        "om_1": ("void", "recalled"),
        "om_2": ("delivered", None),
        "om_3": ("pending", None),
        "om_4": ("void", "stopped"),
    }
    # 作废之后同一条消息再读到也不会重新入箱。
    assert not await inbox.add(
        db_session, bot_id, item("om_1"), Level.IMMEDIATE, owner_open_id=OWNER, hold_until=None
    )
