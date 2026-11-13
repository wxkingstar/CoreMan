"""投递层的表与列：默认值、随记录删除、迁移回填。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import (
    AssistantDelivery,
    AssistantInbox,
    ChatSession,
    FeishuPersonalGrant,
)
from tests.integration.assistant_helpers import raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_delivery_state_defaults_and_goes_with_the_record(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantDelivery(bot_id=bot.id))
    await db_session.commit()
    row = await db_session.get(AssistantDelivery, bot.id, populate_existing=True)
    assert row is not None and row.handover_seq == 0
    assert row.budget_notified_on is None and row.reauth_reminded_on is None
    assert row.next_sweep_at is not None and row.created_at is not None
    await db_session.execute(text("DELETE FROM bots WHERE id = :id"), {"id": bot.id})
    await db_session.commit()
    assert await raw(db_session, "SELECT count(*) FROM assistant_delivery") == 0


async def test_the_added_columns_default_to_nothing(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add_all(
        [
            ChatSession(
                bot_id=bot.id,
                session_key="oc_private",
                relay_session_id=uuid.uuid4(),
                backend="claude",
            ),
            AssistantInbox(bot_id=bot.id, level="immediate", message_id="om_1"),
            FeishuPersonalGrant(
                bot_id=bot.id,
                user_id=owner.id,
                app_id="cli_example",
                platform_user_id="owner_pid",
                open_id="ou_owner",
                tenant_key="tenant-example",
            ),
        ]
    )
    await db_session.commit()
    where = {"bot": bot.id}
    day = "SELECT local_day FROM chat_sessions WHERE bot_id = :bot"
    assert (await db_session.execute(text(day), where)).scalar_one() is None
    assert (
        await raw(db_session, "SELECT failures FROM assistant_inbox WHERE bot_id = :bot", **where)
        == 0
    )
    at = "SELECT authorized_at FROM feishu_personal_grants WHERE bot_id = :bot"
    assert (await db_session.execute(text(at), where)).scalar_one() is None
