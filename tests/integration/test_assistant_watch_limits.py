"""盯消息与限频：被限频时按等待秒数退避、同一应用的人一起停下；用量高时拉长间隔；

读到一半失败时令牌与已读的进度不丢。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import watch
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import ratelimit
from coreman.core.timeutils import utcnow
from tests.integration.watch_helpers import (
    APP_ID,
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
TEAM = {"chat_id": "oc_team", "chat_mode": "group"}


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


async def test_a_rate_limit_backs_off_and_stops_everyone_on_the_app(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    first, second = await assistant(db_session, "a"), await assistant(db_session, "b")
    base = base_time()
    await primed(db_session, first, after(base, -1))  # 先到期的先轮询
    await primed(db_session, second, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    api.limited["/im/v1/messages"] = 30
    monkeypatch.setattr(watch, "CONCURRENCY", 1)
    factory = make_session_factory(db_engine)
    now = after(base, 30)
    with capture_logs() as logs:
        counts = await tick(factory, now)
    assert counts["assistant_watch_backoff"] == 2 and counts["assistant_watch_polled"] == 0
    limited = await watch_row(db_session, first)
    assert (limited.state, limited.last_error, limited.failures) == ("backoff", "rate_limited", 1)
    assert limited.next_poll_at == now + timedelta(seconds=30) and limited.last_ok_at is None
    # 同一个应用的另一位：额度已被占满，这一轮一次飞书都不调。
    waiting = await watch_row(db_session, second)
    assert (waiting.state, waiting.last_error, waiting.failures) == (
        "backoff",
        "budget_exhausted",
        0,
    )
    assert now < waiting.next_poll_at <= now + timedelta(seconds=60)
    assert {hit.token for hit in api.hits} == {first.token}
    assert await ratelimit.Budget(APP_ID).load(db_session, now) >= 1.0
    events = [entry["event"] for entry in logs]
    assert "assistant_watch_rate_limited" in events and "assistant_watch_budget_tight" in events
    # 等待结束之后两位都恢复。
    api.limited.clear()
    counts = await tick(factory, after(now, 61))
    assert counts["assistant_watch_polled"] == 2 and counts["assistant_watch_items"] == 2
    for who in (first, second):
        row = await watch_row(db_session, who)
        assert (row.state, row.last_error, row.failures) == ("polling", None, 0)


async def test_a_token_refreshed_in_a_limited_round_is_kept(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.limited["/im/v1/messages"] = 30
    await tick(make_session_factory(db_engine), after(base, 30))
    # 列会话时换到了新令牌（旧的刷新令牌已用掉），随后读历史被限频：这一轮照常提交。
    assert api.refreshes == 1
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    assert (await watch_row(db_session, who)).last_error == "rate_limited"


@pytest.mark.parametrize(("units", "interval"), [(None, 60), (650, 120), (850, 240)])
async def test_a_tight_budget_stretches_the_interval(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    units: int | None,
    interval: int,
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    now = after(base, 30)
    if units is not None:
        await ratelimit.Budget(APP_ID).record(
            db_session, "history", at=now - timedelta(seconds=5), units=units
        )
        await db_session.commit()
    with capture_logs() as logs:
        counts = await tick(make_session_factory(db_engine), now)
    # 接近上限时照常轮询，只是下一次来得晚。
    assert counts["assistant_watch_polled"] == 1 and "/im/v1/chats" in api.paths()
    row = await watch_row(db_session, who)
    assert (row.state, row.interval) == ("polling", interval)
    assert now < row.next_poll_at <= now + timedelta(seconds=interval)
    tight = [entry for entry in logs if entry["event"] == "assistant_watch_budget_tight"]
    assert len(tight) == (0 if units is None else 1)
    if tight:
        assert tight[0]["log_level"] == "warning" and tight[0]["app_id"] == APP_ID
        assert tight[0]["stretch"] == interval // 60


async def test_a_failed_read_backs_off_and_keeps_what_was_already_read(
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
    counts = await tick(factory, now)
    assert counts["assistant_watch_backoff"] == 1
    row = await watch_row(db_session, who)
    # 只留代码：飞书应答里的文字不进库。
    assert (row.state, row.last_error, row.failures) == ("backoff", "feishu_read_failed", 1)
    assert row.next_poll_at == now + timedelta(seconds=120)
    # 读完的会话的条目与游标留下了；顺序没存，下一轮重新比。
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1"]
    assert (
        row.cursor_enc["chats"]["oc_li"]["m"] == ["om_1"]
        and "t" not in row.cursor_enc["chats"]["oc_team"]
    )
    assert row.head_enc == []
    api.failing = set()
    counts = await tick(factory, row.next_poll_at)
    assert counts["assistant_watch_polled"] == 1 and counts["assistant_watch_items"] == 1
    assert [item.message_id for item in await inbox_rows(db_session, who)] == ["om_1", "om_2"]
    assert (await watch_row(db_session, who)).failures == 0


async def test_the_loop_clears_old_usage_rows_about_once_a_minute(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from coreman.core.db.models import FeishuRateUsage

    monkeypatch.setattr(watch, "_purged", 0.0)
    now = base_time()
    budget = ratelimit.Budget(APP_ID)
    await budget.record(db_session, "chats", at=now - timedelta(minutes=16))
    await budget.record(db_session, "chats", at=now - timedelta(minutes=1))
    await db_session.commit()
    factory = make_session_factory(db_engine)
    await tick(factory, now)
    assert await db_session.scalar(select(func.count()).select_from(FeishuRateUsage)) == 1
    # 刚清过：一分钟内不再清。
    await budget.record(db_session, "chats", at=now - timedelta(minutes=20))
    await db_session.commit()
    await tick(factory, now)
    assert await db_session.scalar(select(func.count()).select_from(FeishuRateUsage)) == 2
