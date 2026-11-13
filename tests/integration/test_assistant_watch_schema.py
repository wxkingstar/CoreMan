"""assistant_watch / assistant_inbox / feishu_rate_usage：默认值、约束、去重索引，内容列是密文。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import (
    INBOX_KINDS,
    INBOX_LEVELS,
    INBOX_STATUSES,
    RATE_CATEGORIES,
    WATCH_STATES,
    AssistantInbox,
    AssistantWatch,
    FeishuRateUsage,
)
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


def test_tables_are_registered_for_truncation() -> None:
    for name in ("assistant_watch", "assistant_inbox"):
        assert BUSINESS_TABLES.index(name) < BUSINESS_TABLES.index("bots")
    assert "feishu_rate_usage" in BUSINESS_TABLES
    assert WATCH_STATES == ("idle", "polling", "backoff", "paused", "stopped")
    assert INBOX_KINDS == ("message", "approval_todo", "reminder")
    assert INBOX_LEVELS == ("immediate", "batch", "hold")
    assert INBOX_STATUSES == ("pending", "delivered", "void")
    assert RATE_CATEGORIES == ("chats", "history")


async def test_defaults_and_removal_with_the_record(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantWatch(bot_id=bot.id))
    db_session.add(AssistantInbox(bot_id=bot.id, level="batch"))
    await db_session.commit()
    watch = await db_session.get(AssistantWatch, bot.id, populate_existing=True)
    assert watch is not None
    assert watch.cursor_enc == {} and watch.head_enc is None
    assert (watch.interval, watch.state, watch.failures) == (60, "idle", 0)
    assert watch.next_poll_at is not None and watch.created_at is not None
    assert watch.last_error is None and watch.last_ok_at is None and watch.keepalive_at is None
    item = await db_session.scalar(select(AssistantInbox).execution_options(populate_existing=True))
    assert item is not None and item.id > 0
    assert (item.kind, item.status) == ("message", "pending")
    assert item.chat_id is None and item.message_id is None and item.thread_id is None
    assert item.summary_enc is None and item.hold_until is None
    assert item.delivered_task_id is None and item.void_code is None and item.message_at is None
    await db_session.execute(text("DELETE FROM bots WHERE id = :id"), {"id": bot.id})
    await db_session.commit()
    assert await raw(db_session, "SELECT count(*) FROM assistant_watch") == 0
    assert await raw(db_session, "SELECT count(*) FROM assistant_inbox") == 0


@pytest.mark.parametrize("bad", [{"state": "sleeping"}, {"interval": 59}, {"interval": 3601}])
async def test_watch_values_are_constrained(db_session: AsyncSession, bad: dict[str, Any]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantWatch(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "bad",
    [{"level": "ignore"}, {"level": "batch", "kind": "note"}, {"level": "batch", "status": "x"}],
)
async def test_inbox_values_are_constrained(db_session: AsyncSession, bad: dict[str, Any]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantInbox(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_item_per_message_and_kind(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    other, _ = await seed_assistant(db_session)
    db_session.add_all(
        [
            AssistantInbox(bot_id=bot.id, level="batch", message_id="om_1"),
            # 别的类型、别的助理、没有消息编号的条目都不受这个索引限制。
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder", message_id="om_1"),
            AssistantInbox(bot_id=other.id, level="batch", message_id="om_1"),
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder"),
            AssistantInbox(bot_id=bot.id, level="batch", kind="reminder"),
        ]
    )
    await db_session.commit()
    db_session.add(AssistantInbox(bot_id=bot.id, level="immediate", message_id="om_1"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_head_and_summary_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(AssistantWatch(bot_id=bot.id, head_enc=[f"oc_{SECRET}"]))
    db_session.add(
        AssistantInbox(
            bot_id=bot.id,
            level="immediate",
            chat_id="oc_li",
            message_id="om_1",
            summary_enc={"text": SECRET},
        )
    )
    await db_session.commit()
    head = str(await raw(db_session, "SELECT head_enc::text FROM assistant_watch"))
    summary = str(await raw(db_session, "SELECT summary_enc::text FROM assistant_inbox"))
    assert SECRET not in head and SECRET not in summary
    assert "enc:v2:d1:" in head and "enc:v2:d1:" in summary
    async with make_session_factory(db_engine)() as other:
        watch = await other.get(AssistantWatch, bot.id)
        item = await other.scalar(select(AssistantInbox))
        assert watch is not None and watch.head_enc == [f"oc_{SECRET}"]
        assert item is not None and item.summary_enc == {"text": SECRET}
        # 密钥销毁之后：行还在、定位信息可读，内容按空值处理，不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantWatch, bot.id, populate_existing=True)
        gone = await other.scalar(select(AssistantInbox).execution_options(populate_existing=True))
        assert locked is not None and sealed.is_locked(locked) and locked.head_enc == []
        assert gone is not None and gone.summary_enc is None and gone.message_id == "om_1"


async def test_rate_usage_defaults_and_constraints(db_session: AsyncSession) -> None:
    db_session.add(FeishuRateUsage(app_id="cli_example", category="chats"))
    await db_session.commit()
    row = await db_session.scalar(select(FeishuRateUsage))
    assert row is not None and row.units == 1 and row.at is not None
    for bad in ({"category": "search"}, {"category": "history", "units": 0}):
        db_session.add(FeishuRateUsage(app_id="cli_example", **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
