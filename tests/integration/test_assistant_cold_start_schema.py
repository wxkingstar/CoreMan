"""assistant_settings 的冷启动与编译列：默认为空、约束、索引；新任务类型登记齐全。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import kinds
from coreman.core.db.models import (
    COLD_START_OUTCOMES,
    COLD_START_STEPS,
    TASK_KINDS,
    AssistantSettings,
)
from tests.integration.assistant_helpers import raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
NEW_COLUMNS = (
    "cold_start_started_at",
    "cold_start_step",
    "cold_start_next_at",
    "cold_start_outcome",
    "worked_on",
    "compiled_on",
    "compiled_at",
)


def test_the_cold_start_kind_is_a_platform_turn() -> None:
    assert kinds.COLD_START_KIND == "assistant_cold_start"
    assert kinds.COLD_START_KIND in kinds.PLATFORM_TURN_KINDS
    assert kinds.COLD_START_KIND in kinds.ORIGIN_KINDS
    assert kinds.unattended(kinds.COLD_START_KIND)
    assert kinds.COLD_START_KIND in TASK_KINDS
    assert COLD_START_STEPS == ("voice", "people", "duties", "focus", "hours")
    assert COLD_START_OUTCOMES == ("completed", "skipped", "timeout")


async def test_new_columns_start_empty(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id))
    await db_session.commit()
    row = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    # 已开通的助理不回填：冷启动没开始、没做完，盯消息与投递都不动。
    assert [getattr(row, name) for name in NEW_COLUMNS] == [None] * len(NEW_COLUMNS)
    assert row.cold_start_done_at is None


@pytest.mark.parametrize("bad", [{"cold_start_step": "bogus"}, {"cold_start_outcome": "bogus"}])
async def test_unknown_values_are_rejected(db_session: AsyncSession, bad: dict[str, Any]) -> None:
    bot, _ = await seed_assistant(db_session)
    db_session.add(AssistantSettings(bot_id=bot.id, **bad))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_the_partial_index_covers_unfinished_cold_starts(db_session: AsyncSession) -> None:
    definition = await raw(
        db_session,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'assistant_settings_cold_start_idx'",
    )
    assert "cold_start_started_at" in definition
    assert "cold_start_done_at IS NULL" in definition
