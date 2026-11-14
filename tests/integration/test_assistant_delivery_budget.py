"""每日轮次预算：数的是所有者时区里今天的对话记录；个人值与平台默认取较小者；可以为负。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, settings
from coreman.core.db.models import Setting
from tests.integration.assistant_helpers import seed_standard
from tests.integration.delivery_helpers import ready, spend, turn_at

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
# 上海 11 月 13 日中午。
NOON = datetime(2026, 11, 13, 4, tzinfo=UTC)


async def test_the_platform_default_applies_until_the_owner_has_his_own(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    assert await delivery.default_budget(db_session) == delivery.DEFAULT_BUDGET == 2000
    assert await delivery.budget_left(db_session, bot, TODAY) == 2000
    db_session.add(Setting(key=delivery.BUDGET_KEY, value=50))
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 50
    (await settings.get(db_session, bot.id)).daily_turn_budget = 12
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 12


@pytest.mark.parametrize("bad", [None, "lots", 5, 50000, True])
async def test_a_broken_setting_falls_back_to_the_default(
    db_session: AsyncSession, bad: object
) -> None:
    db_session.add(Setting(key=delivery.BUDGET_KEY, value=bad))
    await db_session.commit()
    assert await delivery.default_budget(db_session) == delivery.DEFAULT_BUDGET


async def test_only_todays_turns_of_this_assistant_count(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    other = await seed_standard(db_session)
    await spend(db_session, bot, 3, NOON)
    db_session.add_all(
        [
            # 上海 12 日 23:59 与 14 日 00:00：都不是 13 日。
            turn_at(bot, datetime(2026, 11, 12, 15, 59, tzinfo=UTC)),
            turn_at(bot, datetime(2026, 11, 13, 16, 0, tzinfo=UTC)),
            turn_at(other, NOON),
        ]
    )
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 1997
    assert await delivery.budget_left(db_session, bot, TODAY + timedelta(days=1)) == 1999


async def test_the_owners_zone_decides_which_day_a_turn_belongs_to(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    # UTC 13 日 03:00：上海是 13 日上午，纽约还是 12 日晚上。
    await spend(db_session, bot, 1, datetime(2026, 11, 13, 3, tzinfo=UTC))
    assert await delivery.budget_left(db_session, bot, TODAY) == 1999
    (await settings.get(db_session, bot.id)).timezone = "America/New_York"
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 2000
    assert await delivery.budget_left(db_session, bot, date(2026, 11, 12)) == 1999


async def test_the_budget_can_go_negative(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    (await settings.get(db_session, bot.id)).daily_turn_budget = 10
    await db_session.commit()
    await spend(db_session, bot, 13, NOON)
    assert await delivery.budget_left(db_session, bot, TODAY) == -3


async def test_a_lowered_platform_default_tightens_personal_budgets_too(
    db_session: AsyncSession,
) -> None:
    """个人值只能把预算调低：管理员把平台默认调到某人自己的值以下时，以较小的为准。"""
    bot, _ = await ready(db_session)
    (await settings.get(db_session, bot.id)).daily_turn_budget = 150
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 150
    db_session.add(Setting(key=delivery.BUDGET_KEY, value=80))
    await db_session.commit()
    assert await delivery.budget_left(db_session, bot, TODAY) == 80
    assert (await delivery.usage(db_session, NOON))[bot.id].budget == 80
    assert delivery.effective_budget(None, 80) == 80
    assert delivery.effective_budget(30, 80) == 30
    assert delivery.effective_budget(150, 80) == 80
