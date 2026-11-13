"""盯消息不被个别坏数据拖住：读不了的会话放一放、积压分几轮读完、坏消息跳过、

意外出错时这一轮里换到的令牌不丢。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import watch
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET
from tests.integration.watch_helpers import (
    Hit,
    assistant,
    inbox_rows,
    install,
    msg,
    primed,
    tick,
    tokens,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
TEAM = {"chat_id": "oc_team", "chat_mode": "group"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


async def test_a_chat_that_cannot_be_read_is_set_aside_after_three_rounds(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_1", after(base, 10))
    api.say("oc_team", "om_2", after(base, 11))
    api.failing = {"oc_team"}
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    with capture_logs() as logs:
        for strikes in (1, 2, 3):
            counts = await tick(factory, now)
            assert counts["assistant_watch_backoff"] == 1
            row = await watch_row(db_session, who)
            assert (row.state, row.last_error, row.failures) == (
                "backoff",
                "feishu_read_failed",
                strikes,
            )
            now = row.next_poll_at
    parked = row.cursor["chats"]["oc_team"]
    assert parked["f"] == watch.CHAT_STRIKES - 1 and parked["x"] > int(now.timestamp() * 1000)
    assert [entry["event"] for entry in logs].count("assistant_watch_chat_parked") == 1
    # 第四轮起不再读它：别的会话照常盯。
    api.hits.clear()
    api.say("oc_li", "om_3", now - timedelta(seconds=1))
    counts = await tick(factory, now)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 1
    assert api.read("oc_team") == [] and len(api.read("oc_li")) == 1
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error, row.failures) == ("polling", None, 0)
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1", "om_3"]
    # 放够了时间、它又有了动静：再试；读成了就照常，之前记的失败作废。
    api.failing = set()
    later = now + watch.PARK + timedelta(seconds=60)
    api.say("oc_team", "om_4", later - timedelta(seconds=5))
    api.chats = [TEAM, LI]
    counts = await tick(factory, later)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 2
    entry = (await watch_row(db_session, who)).cursor["chats"]["oc_team"]
    assert "f" not in entry and "x" not in entry
    assert [item.message_id for item in await inbox_rows(db_session, who)][2:] == ["om_2", "om_4"]


async def test_failures_that_are_not_about_one_chat_do_not_set_it_aside(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.limited["/im/v1/messages"] = 5
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    for _ in range(4):
        await tick(factory, now)
        row = await watch_row(db_session, who)
        assert row.last_error == "rate_limited"
        now = row.next_poll_at + timedelta(seconds=120)
    assert "f" not in row.cursor["chats"]["oc_li"] and "x" not in row.cursor["chats"]["oc_li"]


async def test_a_backlog_longer_than_one_round_is_finished_in_the_next(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [TEAM]
    for n in range(1, 121):
        api.say("oc_team", f"om_{n}", after(base, n))
    factory = make_session_factory(db_engine)
    counts = await tick(factory, after(base, 200))
    # 一个会话一轮最多读两页；后面还有，记下来下一轮接着读。
    assert counts["assistant_watch_items"] == 100 and len(api.read("oc_team")) == 2
    row = await watch_row(db_session, who)
    assert row.cursor["todo"] == ["oc_team"]
    counts = await tick(factory, row.next_poll_at)
    assert counts["assistant_watch_items"] == 20
    row = await watch_row(db_session, who)
    assert row.cursor["todo"] == [] and row.cursor["chats"]["oc_team"]["m"] == ["om_120"]
    assert len(await inbox_rows(db_session, who)) == 120


async def test_messages_that_cannot_be_parsed_are_skipped_and_only_counted(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI, {"name": "没有编号的会话"}, "不是对象"]  # type: ignore[list-item]
    no_id = msg("", after(base, 5), chat="oc_li", text=f"没有编号 {SECRET}")
    no_time = {**msg("om_x", after(base, 6), chat="oc_li", text=SECRET), "create_time": None}
    odd = {
        **msg("om_2", after(base, 8), chat="oc_li", text=SECRET),
        "sender": "不是对象",
        "body": 7,
        "mentions": "ou_owner",
    }
    api.history["oc_li"] = [no_id, no_time, msg("om_1", after(base, 7), chat="oc_li"), odd]
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), after(base, 30))
    # 缺编号、缺时间的两条跳过。发送人与正文不成形的那条照样入箱：认不出是不是真人，按私聊里
    # 的通知攒批，正文只留类型。
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 2
    first, second = await inbox_rows(db_session, who)
    assert (first.message_id, first.level) == ("om_1", "immediate")
    assert (second.message_id, second.level) == ("om_2", "batch")
    assert second.summary_enc == {
        "chat_kind": "p2p",
        "chat_name": "",
        "sender_id": "",
        "sender_type": "",
        "msg_type": "text",
        "text": "[text消息]",
        "mentioned": False,
    }
    skipped = [entry for entry in logs if entry["event"] == "assistant_watch_messages_skipped"]
    assert len(skipped) == 1 and skipped[0]["count"] == 2
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)
    row = await watch_row(db_session, who)
    assert (row.state, row.head_enc) == ("polling", ["oc_li"])


async def test_an_unexpected_error_keeps_the_token_refreshed_in_that_round(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新令牌一次性使用：列会话时换到了新令牌，随后意外出错也要把它存下，否则授权就此作废。"""
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]

    async def before(hit: Hit) -> None:
        if hit.path == "/im/v1/messages":
            raise RuntimeError(f"synthetic {SECRET}")

    api.before = before
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    assert counts["assistant_watch_failed"] == 1 and api.refreshes == 1
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error, row.failures) == ("backoff", "RuntimeError", 1)
    assert row.next_poll_at == now + timedelta(seconds=watch.ERROR_BACKOFF_MAX)
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)


async def test_a_round_cancelled_midway_finishes_and_keeps_the_refreshed_token(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduler 停止时会取消循环：正在进行的一轮刚换过令牌，要等它提交完，不能就地回滚。"""
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    reached, release = asyncio.Event(), asyncio.Event()

    async def before(hit: Hit) -> None:
        reached.set()
        await release.wait()

    api.before = before
    now = after(base, 30)
    job = asyncio.create_task(tick(make_session_factory(db_engine), now))
    await asyncio.wait_for(reached.wait(), 5)
    # 列会话之前已经换到了新令牌（旧的刷新令牌已经用掉），这时循环被取消。
    assert api.refreshes == 1
    job.cancel()
    await asyncio.sleep(0.05)
    assert not job.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    row = await watch_row(db_session, who)
    assert (row.state, row.last_ok_at) == ("polling", now)


async def test_a_cancelled_round_is_not_waited_for_forever(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    reached, release = asyncio.Event(), asyncio.Event()

    async def before(hit: Hit) -> None:
        reached.set()
        await release.wait()

    api.before = before
    monkeypatch.setattr(watch, "STOP_GRACE", 0.1)
    job = asyncio.create_task(tick(make_session_factory(db_engine), after(base, 30)))
    await asyncio.wait_for(reached.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    release.set()
