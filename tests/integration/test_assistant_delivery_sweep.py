"""顺带检查的循环：每位助理五分钟一次；授权快满一年时每天私信一次；一位出错不挡别人。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import delivery, settings
from coreman.core.assistants import delivery_checks as checks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantDelivery,
    AssistantInbox,
    AssistantMark,
    Bot,
    FeishuPersonalGrant,
    OutboxItem,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.delivery_helpers import boxed, inbox_rows, ready
from tests.integration.lifecycle_helpers import CIPHER
from tests.integration.test_assistant_delivery_feishu_checks import Feishu
from tests.integration.watch_helpers import Hit, assistant, install, tokens
from tests.integration.worker_helpers import MASTER

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
# 明天上海的中午：离所有者的午夜足够远，用例里加几分钟不会跨天。取将来的时刻，是因为投递
# 状态行建出来时 `next_sweep_at` 是数据库的现在——用例的「现在」得在它之后才认领得到。
NOON = datetime.combine(utcnow().date() + timedelta(days=1), time(4), tzinfo=UTC)
DAY = NOON.date().isoformat()
REMINDERS = (
    "SELECT count(*) FROM outbox WHERE bot_id = :bot AND dedupe_key LIKE 'assistant-reauth:%'"
)


async def authorized(session: AsyncSession, bot: Bot, at: datetime | None) -> None:
    await session.execute(
        update(FeishuPersonalGrant)
        .where(FeishuPersonalGrant.bot_id == bot.id)
        .values(authorized_at=at)
    )
    await session.commit()


async def remind(session: AsyncSession, bot: Bot, now: datetime) -> int:
    prefs = await settings.get(session, bot.id)
    row = await delivery.state(session, bot.id)
    assert row is not None
    sent = await checks.remind_reauth(session, bot, prefs, row, now)
    await session.commit()
    return sent


async def last_notice(session: AsyncSession, bot: Bot) -> dict[str, Any]:
    """最近一条授权到期提醒：发到哪、说了什么（按主键取行，载荷自动还原）。"""
    item_id = await session.scalar(
        select(OutboxItem.id)
        .where(OutboxItem.bot_id == bot.id, OutboxItem.dedupe_key.like("assistant-reauth:%"))
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    row = await session.get(OutboxItem, item_id, populate_existing=True)
    assert row is not None
    return {"target": row.target, "text": row.payload["markdown"]}


def test_completing_an_authorization_records_when_and_a_refresh_keeps_it() -> None:
    row = FeishuPersonalGrant(
        bot_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        app_id="cli_example",
        platform_user_id="owner_pid",
        open_id="ou_owner",
        tenant_key="tenant-example",
    )
    data = {"access_token": "a", "refresh_token": "r", "expires_in": 7200}
    personal.save_tokens(Cipher(MASTER), row, data, "app-secret", authorized=True)
    first = row.authorized_at
    assert first is not None and first.tzinfo is not None
    personal.save_tokens(Cipher(MASTER), row, {**data, "access_token": "b"}, "app-secret")
    assert row.authorized_at == first


async def test_no_reminder_until_two_weeks_before_the_year_is_up(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await authorized(db_session, bot, NOON - timedelta(days=350))
    assert await remind(db_session, bot, NOON) == 0
    await authorized(db_session, bot, None)
    assert await remind(db_session, bot, NOON) == 0
    assert await raw(db_session, REMINDERS, bot=bot.id) == 0


async def test_the_owner_is_reminded_once_a_day_from_fourteen_days_out(
    db_session: AsyncSession,
) -> None:
    bot, _ = await ready(db_session)
    await authorized(db_session, bot, NOON - timedelta(days=355))
    assert await remind(db_session, bot, NOON) == 1
    assert await remind(db_session, bot, NOON + timedelta(hours=3)) == 0
    assert await raw(db_session, REMINDERS, bot=bot.id) == 1
    notice = await last_notice(db_session, bot)
    assert notice["target"] == {"chat_id": "oc_private"}
    assert "还有 10 天" in notice["text"] and "重新授权" in notice["text"]
    assert await remind(db_session, bot, NOON + timedelta(days=1)) == 1
    assert "还有 9 天" in (await last_notice(db_session, bot))["text"]
    assert await raw(db_session, REMINDERS, bot=bot.id) == 2


async def test_past_the_year_the_reminder_says_so(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await authorized(db_session, bot, NOON - timedelta(days=366))
    assert await remind(db_session, bot, NOON) == 1
    assert (await last_notice(db_session, bot))["text"] == checks.REAUTH_OVERDUE


async def test_a_grant_that_is_not_connected_gets_no_reminder(db_session: AsyncSession) -> None:
    bot, _ = await ready(db_session)
    await authorized(db_session, bot, NOON - timedelta(days=360))
    await db_session.execute(
        update(FeishuPersonalGrant)
        .where(FeishuPersonalGrant.bot_id == bot.id)
        .values(status="revoked")
    )
    await db_session.commit()
    assert await remind(db_session, bot, NOON) == 0


async def stale_mark(session: AsyncSession, bot: Bot) -> None:
    session.add(
        AssistantMark(
            bot_id=bot.id,
            message_id="om_old",
            chat_id="oc_li",
            reaction_id="r1",
            created_at=NOON - timedelta(hours=5),
        )
    )
    await session.commit()


async def next_sweep(session: AsyncSession, bot: Bot) -> datetime:
    row = await session.get(AssistantDelivery, bot.id, populate_existing=True)
    assert row is not None
    return row.next_sweep_at


async def test_each_assistant_is_checked_every_five_minutes(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await ready(db_session)
    await stale_mark(db_session, bot)
    await authorized(db_session, bot, NOON - timedelta(days=360))
    # 一条已经交回的消息：要读飞书的检查会查它。
    await boxed(db_session, bot, "om_1")
    task = await delivery.dispatch(db_session, bot, NOON)
    assert task is not None
    await delivery.settle(db_session, task, True)
    # 记账时间是数据库的现在；用例的「现在」是固定的那天中午，把它对齐过去。
    await db_session.execute(text("UPDATE assistant_inbox SET updated_at = :at"), {"at": NOON})
    await db_session.commit()
    feishu = Feishu()

    async def stub(*_args: Any) -> Feishu:
        return feishu

    monkeypatch.setattr(checks, "reader", stub)
    factory = make_session_factory(db_engine)
    with capture_logs() as logs:
        counts = await checks.tick(factory, CIPHER, NOON)
    assert counts["assistant_marks_stale"] == 1 and counts["assistant_reauth_reminded"] == 1
    assert counts["assistant_followups_due"] == 0 and counts["assistant_recalls_noticed"] == 0
    assert feishu.calls == ["im.mget"]
    assert await next_sweep(db_session, bot) == NOON + checks.SWEEP_EVERY
    assert SECRET not in str(logs)
    # 没到点：不认领。
    again = await checks.tick(factory, CIPHER, NOON + timedelta(minutes=4))
    assert not any(again.values()) and feishu.calls == ["im.mget"]
    # 到点再查：当天提过的不再入箱、不再私信。
    later = await checks.tick(factory, CIPHER, NOON + timedelta(minutes=5))
    assert not any(later.values()) and feishu.calls == ["im.mget", "im.mget"]
    reminders = [row for row in await inbox_rows(db_session, bot) if row.kind == "reminder"]
    assert [row.message_id for row in reminders] == [f"mark:om_old:{DAY}"]
    assert await raw(db_session, REMINDERS, bot=bot.id) == 1


@pytest.mark.parametrize("change", ["off_shift", "cold_start"])
async def test_an_assistant_that_is_resting_is_not_checked(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    bot, _ = await ready(db_session)
    await stale_mark(db_session, bot)
    await authorized(db_session, bot, NOON - timedelta(days=360))
    prefs = await settings.get(db_session, bot.id)
    if change == "off_shift":
        prefs.shift_override = "off"
    else:
        prefs.cold_start_done_at = None
    await db_session.commit()
    feishu = Feishu()

    async def stub(*_args: Any) -> Feishu:
        return feishu

    monkeypatch.setattr(checks, "reader", stub)
    counts = await checks.tick(make_session_factory(db_engine), CIPHER, NOON)
    assert not any(counts.values()) and feishu.calls == []
    assert await inbox_rows(db_session, bot) == []
    assert await raw(db_session, REMINDERS, bot=bot.id) == 0
    # 仍然往后排，不会每 5 秒看一次。
    assert await next_sweep(db_session, bot) == NOON + checks.SWEEP_EVERY


async def test_without_a_usable_grant_the_local_checks_still_run(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await ready(db_session)
    await stale_mark(db_session, bot)

    async def nobody(*_args: Any) -> None:
        return None

    monkeypatch.setattr(checks, "reader", nobody)
    counts = await checks.tick(make_session_factory(db_engine), CIPHER, NOON)
    assert counts["assistant_marks_stale"] == 1
    assert counts["assistant_approvals_auto_withdrawn"] == 0


async def test_a_failing_check_is_pushed_back_and_logs_only_its_type(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await ready(db_session)

    async def broken(*_args: Any, **_kw: Any) -> dict[str, int]:
        raise RuntimeError(f"synthetic {SECRET}")

    monkeypatch.setattr(checks, "sweep", broken)
    with capture_logs() as logs:
        counts = await checks.tick(make_session_factory(db_engine), CIPHER, NOON)
    assert not any(counts.values())
    assert SECRET not in str(logs) and "RuntimeError" in str(logs)
    assert await next_sweep(db_session, bot) == NOON + checks.SWEEP_EVERY


async def test_an_unexpected_error_after_calling_feishu_keeps_the_refreshed_token(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新令牌一次性使用：检查里换到了新令牌，随后意外出错也要把它存下，不能回滚。"""
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    db_session.add(
        AssistantInbox(bot_id=who.bot.id, level="immediate", message_id="om_1", status="delivered")
    )
    await db_session.commit()

    async def before(hit: Hit) -> None:
        raise RuntimeError(f"synthetic {SECRET}")

    api.before = before
    with capture_logs() as logs:
        # 状态行的 `next_sweep_at` 是登记那一刻数据库的现在：用例的「现在」取在它之后。
        now = utcnow() + timedelta(minutes=1)
        counts = await checks.tick(make_session_factory(db_engine), CIPHER, now)
    assert api.paths() == ["/im/v1/messages/mget"] and api.refreshes == 1
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    assert counts["assistant_recalls_noticed"] == 0
    assert SECRET not in str(logs) and "RuntimeError" in str(logs)


async def test_a_check_cancelled_midway_finishes_and_keeps_the_refreshed_token(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduler 停止时会取消循环：正在检查的这一位刚换过令牌，要等它提交完。"""
    api = install(monkeypatch)
    who = await assistant(db_session, expired=True)
    db_session.add(
        AssistantInbox(bot_id=who.bot.id, level="immediate", message_id="om_1", status="delivered")
    )
    await db_session.commit()
    reached, release = asyncio.Event(), asyncio.Event()

    async def before(hit: Hit) -> None:
        reached.set()
        await release.wait()

    api.before = before
    now = utcnow() + timedelta(minutes=1)
    job = asyncio.create_task(checks.tick(make_session_factory(db_engine), CIPHER, now))
    await asyncio.wait_for(reached.wait(), 5)
    assert api.refreshes == 1
    job.cancel()
    await asyncio.sleep(0.05)
    assert not job.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    assert (await tokens(db_session, who))["refresh_token"] == "u-refresh-r1"
    row = await db_session.get(AssistantDelivery, who.bot.id, populate_existing=True)
    assert row is not None and row.next_sweep_at == now + checks.SWEEP_EVERY
