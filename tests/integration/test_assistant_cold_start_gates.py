"""冷启动做完之前盯消息与投递都不动；做完之后按上下班；停用时清掉本层的状态。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import cold_start, delivery, deprovisioning, settings, watch
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantSettings
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import FakeMessenger, install, tick
from tests.integration.shift_helpers import last_sent
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import MASTER

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
CIPHER = Cipher(MASTER)


async def test_watch_and_delivery_start_only_after_the_cold_start(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    prefs = await settings.get(db_session, bot.id)
    # 排除「不在工作时段」这个原因：只看冷启动这一道门。
    prefs.shift_override = "on"
    assert await watch._resting(db_session, bot, prefs, now) == ("idle", "cold_start_pending")
    assert not delivery.gate(bot, prefs, now)
    assert await cold_start.begin(db_session, bot.id, now)
    prefs.cold_start_step = "focus"
    assert await watch._resting(db_session, bot, prefs, now) == ("idle", "cold_start_pending")
    assert not delivery.gate(bot, prefs, now)
    # 做完（跳过、超时同样）之后才开始。
    assert await cold_start.complete(db_session, bot, prefs, "skipped", now)
    assert await watch._resting(db_session, bot, prefs, now) is None
    assert delivery.gate(bot, prefs, now)


async def test_after_the_cold_start_both_follow_the_shift(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    prefs = await settings.get(db_session, bot.id)
    await cold_start.begin(db_session, bot.id, now)
    await cold_start.complete(db_session, bot, prefs, "completed", now)
    until = settings.clock_out(prefs, now)
    assert await watch._resting(db_session, bot, prefs, now) == ("idle", None)
    assert not delivery.gate(bot, prefs, now)
    settings.clock_in(prefs, now)
    assert await watch._resting(db_session, bot, prefs, now) is None
    assert delivery.gate(bot, prefs, now)
    assert until > now


def test_the_focus_limit_matches_what_the_watch_reads() -> None:
    assert settings.MAX_FOCUS == watch.FOCUS_MAX


async def test_stopping_the_assistant_clears_cold_start_and_switch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner = await assistant(db_session)
    start = utcnow()
    prefs = await settings.get(db_session, bot.id)
    await cold_start.begin(db_session, bot.id, start)
    prefs.cold_start_step = "duties"
    settings.clock_in(prefs, start)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    await tick(factory, FakeMessenger())  # 停用的第一步
    stored = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert stored is not None
    assert stored.cold_start_step is None and stored.cold_start_next_at is None
    assert stored.shift_override is None and stored.shift_override_until is None
    # 没做完的冷启动不会在 24 小时后被兜底成「完成」，也不发消息。
    late = start + cold_start.DEADLINE + timedelta(hours=1)
    assert await cold_start.tick(factory, CIPHER, late) == {}
    stored = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert stored is not None and stored.cold_start_done_at is None
    sent = await last_sent(db_session, bot)
    assert sent is None or "24 小时" not in str(sent.payload)
