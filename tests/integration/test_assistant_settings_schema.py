"""assistant_settings：默认值、约束、随记录删除，以及两列会话名单在库里是密文。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import SHIFT_OVERRIDES, AssistantSettings
from coreman.core.db.session import make_session_factory
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT coalesce(focus_chats_enc::text, '') || coalesce(ignored_chats_enc::text, '')"
    " FROM assistant_settings WHERE bot_id = :id"
)


def test_table_is_truncated_before_bots() -> None:
    assert BUSINESS_TABLES.index("assistant_settings") < BUSINESS_TABLES.index("bots")
    assert SHIFT_OVERRIDES == ("on", "off")


async def test_defaults_and_removal_with_the_record(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id))
    await db_session.commit()
    row = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    assert (row.open_id, row.tenant_key, row.platform_user_id, row.chat_id) == ("", "", "", "")
    assert (row.timezone, row.work_start, row.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert row.workdays == [1, 2, 3, 4, 5]
    assert row.focus_chats_enc is None and row.ignored_chats_enc is None
    assert row.daily_turn_budget is None and row.shift_override is None
    assert row.shift_override_until is None and row.cold_start_done_at is None
    assert row.updated_at is not None
    await db_session.execute(text("DELETE FROM bots WHERE id = :id"), {"id": bot.id})
    await db_session.commit()
    left = await raw(db_session, "SELECT count(*) FROM assistant_settings")
    assert left == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"work_start": "9:30"},
        {"work_end": "24:00"},
        {"work_start": "19:00", "work_end": "09:30"},
        {"work_start": "10:00", "work_end": "10:00"},
        {"workdays": [0]},
        {"workdays": [1, 8]},
        {"shift_override": "maybe"},
        {"daily_turn_budget": 5},
        {"daily_turn_budget": 2001},
    ],
)
async def test_values_are_constrained(db_session: AsyncSession, bad: dict[str, object]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_chat_lists_are_sealed_and_read_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = AssistantSettings(
        bot_id=bot.id, focus_chats_enc=[f"oc_{SECRET}"], ignored_chats_enc=["oc_noise"]
    )
    db_session.add(row)
    await db_session.commit()
    stored = str(await raw(db_session, STORED, id=bot.id))
    assert SECRET not in stored and "oc_noise" not in stored
    assert stored.count("enc:v2:d1:") == 2
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantSettings, bot.id)
        assert found is not None
        assert found.focus_chats_enc == [f"oc_{SECRET}"]
        assert found.ignored_chats_enc == ["oc_noise"]
        # 密钥销毁之后：行还在、其余列可读，名单按空列表处理，不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantSettings, bot.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.focus_chats_enc == [] and locked.ignored_chats_enc == []
        assert locked.timezone == "Asia/Shanghai"
