"""盯消息不倒灌历史：没有游标的会话只回看一小段，移出忽略名单的从移出那一刻读起，太老的消息不入箱。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings, watch
from coreman.core.assistants import watch_feed as feed
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.watch_helpers import (
    Watched,
    assistant,
    inbox_rows,
    install,
    ms,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
TEAM = {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"}
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def ago(base: datetime, **kw: Any) -> datetime:
    return base - timedelta(**kw)


async def boxed(session: AsyncSession, who: Watched) -> list[str]:
    return [str(row.message_id) for row in await inbox_rows(session, who)]


async def polled(session: AsyncSession, who: Watched, at: datetime) -> None:
    """上一次轮询成功的时刻，并让这一行立刻到期。"""
    row = await watch_row(session, who)
    row.last_ok_at, row.next_poll_at = at, at
    await session.commit()


def test_the_windows_are_the_agreed_ones() -> None:
    assert watch.LOOKBACK == timedelta(minutes=30)
    assert watch.MAX_AGE == timedelta(hours=24)


async def test_a_group_the_owner_was_just_added_to_does_not_dump_its_history(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    # 三天前就开始盯了；这个群有历史，所有者刚被拉进来，游标里没有它。
    await primed(db_session, who, ago(base, days=3), head=["oc_li"])
    await polled(db_session, who, ago(base, seconds=60))
    api.chats = [TEAM, LI]
    api.say("oc_team", "om_week", ago(base, days=2))
    api.say("oc_team", "om_morning", ago(base, hours=5))
    api.say("oc_team", "om_recent", ago(base, minutes=10))
    api.say("oc_team", "om_now", ago(base, seconds=20))
    await tick(make_session_factory(db_engine), base)
    # 只回看 30 分钟：更早的历史不入箱，也不会在之后的轮次里补进来。
    assert await boxed(db_session, who) == ["om_recent", "om_now"]
    start = int(api.read("oc_team")[0].params["start_time"])
    assert start == ms(base - watch.LOOKBACK) // 1000
    row = await watch_row(db_session, who)
    assert row.cursor_enc["chats"]["oc_team"]["t"] == ms(ago(base, seconds=20))
    assert row.cursor_enc["since"] == ms(ago(base, days=3))


async def test_a_chat_pushed_out_of_the_cursor_restarts_from_the_window(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, ago(base, days=3), head=["oc_team"])
    # 游标只留最近读过的 CURSOR_CHATS 个会话：这个群很久没动静，被挤掉了。
    row = await watch_row(db_session, who)
    chats = {
        f"oc_{index}": {"t": ms(ago(base, hours=1)), "m": [], "k": "group", "th": {}}
        for index in range(feed.CURSOR_CHATS)
    }
    chats["oc_team"] = {"t": ms(ago(base, days=2)), "m": ["om_read"], "k": "group", "th": {}}
    row.cursor_enc = feed.dump({"since": ms(ago(base, days=3)), "chats": chats}, [])
    await db_session.commit()
    assert "oc_team" not in (await watch_row(db_session, who)).cursor_enc["chats"]
    await polled(db_session, who, ago(base, seconds=60))
    api.chats = [TEAM]
    # 被挤掉之前读过的、当时分级为忽略没入箱的旧消息，不该在它再活跃时整批回来。
    api.say("oc_team", "om_read", ago(base, days=2))
    api.say("oc_team", "om_quiet", ago(base, hours=20))
    api.say("oc_team", "om_new", ago(base, seconds=20))
    await tick(make_session_factory(db_engine), base)
    assert await boxed(db_session, who) == ["om_new"]


async def test_a_chat_taken_off_the_ignore_list_is_read_from_that_moment(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, ago(base, days=3), head=["oc_team"])
    await settings.update(db_session, who.bot.id, now=ago(base, hours=2), ignored_chats=["oc_team"])
    await db_session.commit()
    # 忽略期间别的会话照常；这个群的游标停在一小时前（忽略之前读到的位置）。
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {
            "oc_team": {
                "t": ms(ago(base, hours=3)),
                "m": ["om_old"],
                "k": "group",
                "th": {"omt_1": ms(ago(base, hours=3))},
                "o": ms(ago(base, hours=4)),
            }
        },
    }
    await db_session.commit()
    released = ago(base, minutes=5)
    await settings.update(db_session, who.bot.id, now=released, ignored_chats=[])
    await db_session.commit()
    entry = (await watch_row(db_session, who)).cursor_enc["chats"]["oc_team"]
    assert (entry["t"], entry["m"], entry["th"]) == (ms(released), [], {})
    assert entry["k"] == "group" and entry["o"] == ms(ago(base, hours=4))
    await polled(db_session, who, ago(base, seconds=60))
    api.chats = [TEAM]
    api.say("oc_team", "om_ignored_1", ago(base, hours=2))
    api.say("oc_team", "om_ignored_2", ago(base, minutes=6))
    api.say("oc_team", "om_after", ago(base, minutes=4))
    await tick(make_session_factory(db_engine), base)
    # 忽略期间的消息不入箱（哪怕在回看窗口里）；移出之后的才入。
    assert await boxed(db_session, who) == ["om_after"]


async def test_removing_a_chat_the_watch_never_read_starts_it_now(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, ago(base, days=3))
    await settings.update(db_session, who.bot.id, now=ago(base, hours=2), ignored_chats=["oc_team"])
    await settings.update(db_session, who.bot.id, now=ago(base, minutes=5), ignored_chats=[])
    await db_session.commit()
    entry = (await watch_row(db_session, who)).cursor_enc["chats"]["oc_team"]
    assert entry == {"t": ms(ago(base, minutes=5)), "m": [], "th": {}}
    # 还没开始盯（没有基线）的助理没有游标可改：照常保存设置。
    other = await assistant(db_session, "b")
    await settings.update(db_session, other.bot.id, now=base, ignored_chats=["oc_x"])
    await settings.update(db_session, other.bot.id, now=base, ignored_chats=[])
    await db_session.commit()
    assert api.hits == []


async def test_messages_older_than_a_day_only_move_the_cursor(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, ago(base, days=9), head=["oc_li"])
    # 这个会话的游标停在三天前（比如它被放下、或助理停了几天）。
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {"oc_li": {"t": ms(ago(base, days=3)), "m": [], "k": "p2p", "th": {}}},
    }
    await db_session.commit()
    await polled(db_session, who, ago(base, days=3))
    api.chats = [LI]
    api.say("oc_li", "om_stale", ago(base, days=2))
    api.say("oc_li", "om_edge", ago(base, hours=24, seconds=1))
    api.say("oc_li", "om_fresh", ago(base, hours=23))
    counts = await tick(make_session_factory(db_engine), base)
    assert counts["assistant_watch_items"] == 1
    assert await boxed(db_session, who) == ["om_fresh"]
    entry = (await watch_row(db_session, who)).cursor_enc["chats"]["oc_li"]
    assert (entry["t"], entry["m"]) == (ms(ago(base, hours=23)), ["om_fresh"])


async def test_after_a_long_pause_an_unseen_chat_is_read_from_the_last_poll(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, ago(base, days=3), head=["oc_team"])
    # 昨晚下班后就没轮询过；夜里一位从没说过话的同事发来了私聊。
    await polled(db_session, who, ago(base, hours=14))
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_before", ago(base, hours=15))
    api.say("oc_li", "om_night", ago(base, hours=9))
    await tick(make_session_factory(db_engine), base)
    # 回看到上一次轮询为止（窗口是下限，不是上限）：夜里那条不能丢，更早的不要。
    assert await boxed(db_session, who) == ["om_night"]
