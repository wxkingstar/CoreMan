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
    assert watch.MAX_LOOKBACK == timedelta(days=7)


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
    # 轮询本身一直没停（一分钟前还成功过）：这些旧消息在上次轮询之前就超过一天了。
    await polled(db_session, who, ago(base, seconds=60))
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


async def test_messages_from_the_weekend_arrive_on_monday_morning(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    # 周五 19:00 下班（最后一次成功轮询），周一 09:00 上班：中间 62 小时没有轮询。
    friday = ago(base, hours=62)
    await primed(db_session, who, ago(base, days=9), head=["oc_li", "oc_team"])
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {
            "oc_li": {"t": ms(ago(base, hours=63)), "m": [], "k": "p2p", "th": {}},
            "oc_team": {"t": ms(ago(base, hours=63)), "m": [], "k": "group", "th": {}},
        },
    }
    await db_session.commit()
    await polled(db_session, who, friday)
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_friday_night", ago(base, hours=60))
    api.say("oc_li", "om_saturday", ago(base, hours=40))
    api.say("oc_li", "om_sunday", ago(base, hours=10))
    api.say(
        "oc_team",
        "om_at_saturday",
        ago(base, hours=45),
        mentions=[{"id": who.open_id, "id_type": "open_id"}],
    )
    await tick(make_session_factory(db_engine), base)
    # 平台没轮询的这段时间里发来的，都没见过：不因为超过一天而丢掉。
    assert await boxed(db_session, who) == [
        "om_friday_night",
        "om_saturday",
        "om_sunday",
        "om_at_saturday",
    ]


async def test_a_group_joined_over_the_weekend_still_keeps_its_old_history_out(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    friday = ago(base, hours=62)
    await primed(db_session, who, ago(base, days=30), head=["oc_li"])
    await polled(db_session, who, friday)
    api.chats = [TEAM, LI]
    # 这个群有几周的历史，所有者周末才被拉进来。
    api.say("oc_team", "om_weeks", ago(base, days=20))
    api.say("oc_team", "om_last_week", ago(base, days=4))
    api.say("oc_team", "om_weekend", ago(base, hours=30))
    await tick(make_session_factory(db_engine), base)
    assert await boxed(db_session, who) == ["om_weekend"]
    start = int(api.read("oc_team")[0].params["start_time"])
    assert start == ms(friday) // 1000


def night_chats(count: int = 20) -> list[dict[str, Any]]:
    return [{"chat_id": f"oc_p{index:02d}", "chat_mode": "p2p"} for index in range(count)]


async def test_chats_left_for_the_next_round_keep_last_nights_starting_point(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    # 昨晚下班后没轮询过；夜里 20 位从没说过话的同事各发来一条私聊，游标里都没有条目。
    await primed(db_session, who, ago(base, days=3))
    await polled(db_session, who, ago(base, hours=14))
    api.chats = night_chats()
    for index in range(20):
        api.say(f"oc_p{index:02d}", f"om_night_{index:02d}", ago(base, hours=9))
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    # 一轮最多读 `MAX_READS` 次：其余的排到下一轮。
    assert len(await boxed(db_session, who)) == watch.MAX_READS
    row = await watch_row(db_session, who)
    waiting = row.cursor_enc["todo"]
    assert len(waiting) == 20 - watch.MAX_READS
    # 排队的会话当场记下这一轮的回看起点：下一轮「上次成功轮询」已经变成刚才。
    for chat in waiting:
        assert row.cursor_enc["chats"][chat]["t"] == ms(ago(base, hours=14))
    row.next_poll_at = base
    await db_session.commit()
    await tick(factory, base + timedelta(seconds=60))
    assert sorted(await boxed(db_session, who)) == [f"om_night_{index:02d}" for index in range(20)]
    row = await watch_row(db_session, who)
    assert row.cursor_enc["todo"] == []
    assert all("a" not in entry for entry in row.cursor_enc["chats"].values())


async def test_chats_left_for_the_next_round_keep_the_weekend_too(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    friday = ago(base, hours=62)
    await primed(db_session, who, ago(base, days=9))
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {
            f"oc_p{index:02d}": {"t": ms(ago(base, hours=63)), "m": [], "k": "p2p", "th": {}}
            for index in range(20)
        },
    }
    await db_session.commit()
    await polled(db_session, who, friday)
    api.chats = night_chats()
    for index in range(20):
        api.say(f"oc_p{index:02d}", f"om_friday_{index:02d}", ago(base, hours=60))
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    row = await watch_row(db_session, who)
    row.next_poll_at = base
    await db_session.commit()
    # 第二轮时「上次成功轮询」是一分钟前：排队的会话仍按排队那一轮的年龄截止点算。
    await tick(factory, base + timedelta(seconds=60))
    assert sorted(await boxed(db_session, who)) == [f"om_friday_{index:02d}" for index in range(20)]


async def test_chats_the_round_never_reached_keep_the_weekend_too(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """排在前面的会话把一轮用完：后面顺序没变、还没探到的会话下一轮照样读，周末的消息不丢。"""
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    friday = ago(base, hours=62)
    await primed(db_session, who, ago(base, days=9), head=["oc_li", "oc_team"])
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {
            "oc_li": {"t": ms(ago(base, hours=63)), "m": [], "k": "p2p", "th": {}},
            "oc_team": {"t": ms(ago(base, hours=63)), "m": [], "k": "group", "th": {}},
        },
    }
    await db_session.commit()
    await polled(db_session, who, friday)
    # 周末有 12 位没说过话的同事发来私聊，排到了最前面；原有的两个会话名次没变，其中一个
    # 周六来过消息。
    api.chats = [*night_chats(watch.MAX_READS), LI, TEAM]
    for index in range(watch.MAX_READS):
        api.say(f"oc_p{index:02d}", f"om_new_{index:02d}", ago(base, hours=20))
    api.say("oc_li", "om_saturday", ago(base, hours=40))
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    assert "om_saturday" not in await boxed(db_session, who)
    assert api.read("oc_li") == []
    row = await watch_row(db_session, who)
    # 没探到的那一段与这一轮的年龄截止点（上一次成功轮询：周五）记在游标顶层。
    assert row.cursor_enc["p"] == ["oc_li", "oc_team"]
    assert row.cursor_enc["a"] == ms(friday)
    row.next_poll_at = base
    await db_session.commit()
    # 第二轮时「上次成功轮询」是一分钟前，年龄截止点只剩一天：周六那条仍按第一轮的算。
    await tick(factory, base + timedelta(seconds=60))
    assert "om_saturday" in await boxed(db_session, who)
    # 这一段探到了没有新消息的会话：探完了，顶层的记号去掉。
    assert len(api.read("oc_li")) == 1 and len(api.read("oc_team")) == 1
    assert len(api.read("oc_p00")) == 1
    row = await watch_row(db_session, who)
    assert "p" not in row.cursor_enc and "a" not in row.cursor_enc


async def test_an_unreached_chat_read_by_another_route_does_not_end_the_stretch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没探到的那一段里，有会话下一轮因为来了新消息排到前面被读掉：它后面的仍要探到。"""
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    friday = ago(base, hours=62)
    await primed(db_session, who, ago(base, days=9), head=["oc_li", "oc_team"])
    await polled(db_session, who, friday)
    api.chats = [*night_chats(watch.MAX_READS), LI, TEAM]
    api.say("oc_li", "om_li_saturday", ago(base, hours=40))
    api.say(
        "oc_team",
        "om_team_saturday",
        ago(base, hours=39),
        mentions=[{"id": who.open_id, "id_type": "open_id"}],
    )
    factory = make_session_factory(db_engine)
    await tick(factory, base)
    assert await boxed(db_session, who) == []
    # 一分钟里那位同事又发了一条，他的会话排到了最前面。
    later = base + timedelta(seconds=60)
    api.chats = [LI, *night_chats(watch.MAX_READS), TEAM]
    api.say("oc_li", "om_li_monday", ago(later, seconds=5))
    row = await watch_row(db_session, who)
    row.next_poll_at = base
    await db_session.commit()
    await tick(factory, later)
    assert await boxed(db_session, who) == ["om_li_saturday", "om_li_monday", "om_team_saturday"]
    assert "p" not in (await watch_row(db_session, who)).cursor_enc


@pytest.mark.parametrize(
    ("days", "kept"),
    [
        (3, ["om_li_old", "om_li_recent", "om_team_old", "om_team_recent"]),
        (20, ["om_li_recent", "om_team_edge", "om_team_recent"]),
    ],
)
async def test_coming_back_after_a_long_stop_looks_back_a_week_at_most(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    days: int,
    kept: list[str],
) -> None:
    """停了几天（周末、假期）全补回；停了两三周，只交回最近 7 天的。"""
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    stopped = ago(base, days=days)
    await primed(db_session, who, ago(base, days=40), head=["oc_li"])
    # 一个读过的会话（游标停在停止之前），一个停止期间才有人找来的会话（游标里没有）。
    row = await watch_row(db_session, who)
    row.cursor_enc = {
        **row.cursor_enc,
        "chats": {"oc_li": {"t": ms(stopped) - 1000, "m": [], "k": "p2p", "th": {}}},
    }
    await db_session.commit()
    await polled(db_session, who, stopped)
    api.chats = [TEAM, LI]
    mention = [{"id": who.open_id, "id_type": "open_id"}]
    # 「旧」的那条在停止后一天发出：停 3 天时是两天前，停 20 天时是 19 天前。
    api.say("oc_li", "om_li_old", stopped + timedelta(days=1))
    api.say("oc_li", "om_li_week", ago(base, days=7, hours=1))
    api.say("oc_li", "om_li_recent", ago(base, hours=30))
    api.say("oc_team", "om_team_old", stopped + timedelta(days=1), mentions=mention)
    api.say("oc_team", "om_team_edge", ago(base, days=6, hours=23), mentions=mention)
    api.say("oc_team", "om_team_recent", ago(base, hours=30), mentions=mention)
    await tick(make_session_factory(db_engine), base)
    assert sorted(await boxed(db_session, who)) == kept
    # 没见过的会话从哪一刻读起：停得久时不早于 7 天前。
    start = int(api.read("oc_team")[0].params["start_time"])
    assert start == ms(max(stopped, base - watch.MAX_LOOKBACK)) // 1000
