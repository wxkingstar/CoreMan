"""助理设置的读写：没有就按默认值建一行；开通时记下的身份。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import identified, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_get_creates_one_row_with_defaults(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    first = await settings.get(db_session, bot.id)
    assert (first.timezone, first.work_start, first.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert first.workdays == [1, 2, 3, 4, 5] and not settings.identified(first)
    await db_session.commit()
    # 另一个会话再取：还是那一行，不再建。
    async with make_session_factory(db_engine)() as other:
        again = await settings.get(other, bot.id)
        assert again.bot_id == bot.id
        await other.commit()
    assert await raw(db_session, "SELECT count(*) FROM assistant_settings") == 1


async def test_record_identity_fills_the_four_columns(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    await settings.record_identity(
        db_session,
        bot.id,
        open_id="ou_zhang",
        tenant_key="tenant-example",
        platform_user_id="zhangsan",
        chat_id="oc_zhang",
    )
    await db_session.commit()
    stored = await settings.get(db_session, bot.id)
    assert settings.identified(stored)
    assert (stored.open_id, stored.tenant_key, stored.platform_user_id, stored.chat_id) == (
        "ou_zhang",
        "tenant-example",
        "zhangsan",
        "oc_zhang",
    )
    # 再记一次是覆盖，不多出一行。
    await settings.record_identity(
        db_session,
        bot.id,
        open_id="ou_zhang",
        tenant_key="tenant-example",
        platform_user_id="zhangsan",
        chat_id="oc_zhang_2",
    )
    await db_session.commit()
    assert await raw(db_session, "SELECT chat_id FROM assistant_settings") == "oc_zhang_2"


async def test_helper_matches_the_active_assistant_fixture(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    row = await identified(db_session, bot)
    assert (row.open_id, row.tenant_key, row.platform_user_id, row.chat_id) == (
        "ou_owner",
        "tenant-example",
        "owner_pid",
        "oc_private",
    )
