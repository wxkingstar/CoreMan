"""不轮询时每 24 小时把令牌续上；停用时停止轮询、作废没交回的条目。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import deprovisioning, watch
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.watch_grade import Item, Level
from coreman.core.db.models import AssistantWatch, Bot, FeishuPersonalGrant
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import seed_assistant
from tests.integration.lifecycle_helpers import CIPHER, FakeMessenger
from tests.integration.watch_helpers import (
    Watched,
    assistant,
    inbox_rows,
    install,
    primed,
    tick,
    tokens,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


async def last_alive(session: AsyncSession, who: Watched, at: datetime) -> None:
    await session.execute(
        text("UPDATE assistant_watch SET keepalive_at = :at WHERE bot_id = :id"),
        {"at": at, "id": who.bot.id},
    )
    await session.commit()


@pytest.mark.parametrize("resting", [{"shift": "off"}, {"cold_started": False}])
async def test_a_resting_assistant_refreshes_its_token_once_a_day(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    resting: dict[str, Any],
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, **resting)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    factory = make_session_factory(db_engine)
    # 上次确认令牌还活着是 23 小时前：不刷新。
    await last_alive(db_session, who, base - timedelta(hours=23))
    counts = await tick(factory, base)
    assert counts["assistant_watch_keepalives"] == 0 and api.refreshes == 0
    # 满 24 小时：刷新一次，不调任何业务接口，仍然不轮询。
    await last_alive(db_session, who, base - timedelta(hours=25))
    now = (await watch_row(db_session, who)).next_poll_at
    counts = await tick(factory, now)
    assert counts["assistant_watch_keepalives"] == 1 and counts["assistant_watch_polled"] == 0
    assert api.refreshes == 1 and api.hits == []
    row = await watch_row(db_session, who)
    assert row.state == "idle" and row.keepalive_at == now and row.last_ok_at is None
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    # 刚刷新过：下一次来看时不再刷新。
    await tick(factory, row.next_poll_at)
    assert api.refreshes == 1


async def test_a_new_row_counts_from_its_creation(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, shift="off")
    base = base_time()
    await primed(db_session, who, base)
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    assert api.refreshes == 0
    # 从没确认过（keepalive_at 为空）就从这一行建出来的时刻算起。
    await db_session.execute(
        text("UPDATE assistant_watch SET created_at = :at, next_poll_at = :due"),
        {"at": base - timedelta(hours=25), "due": base},
    )
    await db_session.commit()
    await tick(factory, base + timedelta(seconds=1))
    assert api.refreshes == 1


async def test_a_failed_keepalive_backs_off_and_keeps_the_grant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, shift="off")
    base = base_time()
    await primed(db_session, who, base)
    await last_alive(db_session, who, base - timedelta(hours=25))
    api.limited[personal.TOKEN_URL] = 40
    await tick(make_session_factory(db_engine), base)
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error) == ("backoff", "rate_limited")
    assert row.next_poll_at == base + timedelta(seconds=40)
    assert row.keepalive_at == base - timedelta(hours=25)
    grant = await db_session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert grant is not None and grant.status == "connected"


async def test_a_disabled_record_is_not_kept_alive(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    await last_alive(db_session, who, base - timedelta(hours=25))
    bot = await db_session.get(Bot, who.bot.id)
    assert bot is not None
    bot.enabled = False
    await db_session.commit()
    await tick(make_session_factory(db_engine), base)
    assert api.refreshes == 0 and api.hits == []


def boxed(mid: str) -> Item:
    return Item("oc_li", "p2p", mid, "ou_li", "user", int(utcnow().timestamp() * 1000))


async def test_stop_ends_polling_and_voids_what_was_not_handed_over(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base, head=["oc_li"])
    for mid in ("om_1", "om_2"):
        await inbox.add(
            db_session,
            who.bot.id,
            boxed(mid),
            Level.IMMEDIATE,
            owner_open_id=who.open_id,
            hold_until=None,
        )
    await db_session.commit()
    (await inbox_rows(db_session, who))[1].status = "delivered"
    await db_session.commit()
    await watch.stop(db_session, who.bot.id)
    await watch.stop(db_session, who.bot.id)  # 幂等
    await db_session.commit()
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error, row.cursor, row.head_enc) == ("stopped", None, {}, None)
    found = [(item.status, item.void_code) for item in await inbox_rows(db_session, who)]
    assert found == [("void", "stopped"), ("delivered", None)]
    # 停了之后不再轮询，也不会被重新登记。
    api.chats = [LI]
    counts = await tick(make_session_factory(db_engine), base + timedelta(hours=30))
    assert api.hits == [] and api.refreshes == 0 and counts["assistant_watch_enrolled"] == 0
    assert (await watch_row(db_session, who)).state == "stopped"
    # 从没被盯过的助理：什么都不做，也不建行。
    other, _ = await seed_assistant(db_session)
    await watch.stop(db_session, other.id)
    await db_session.commit()
    assert await db_session.get(AssistantWatch, other.id) is None


async def test_deprovisioning_stops_the_watch_before_draining(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    who = await assistant(db_session)
    await primed(db_session, who, base_time())
    bot = await db_session.get(Bot, who.bot.id)
    assert bot is not None
    flow = await deprovisioning.request(db_session, bot, trigger="owner", actor_id=who.owner.id)
    await db_session.commit()
    assert flow is not None and flow.step == "drain"
    await deprovisioning.step_drain(db_session, CIPHER, flow, FakeMessenger())
    await db_session.commit()
    assert (await watch_row(db_session, who)).state == "stopped"
