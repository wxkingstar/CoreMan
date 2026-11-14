"""私聊里的「上班」「下班」：只有所有者在私聊里发才生效，不经模型，不碰排队中的轮次。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import platform_turns, settings
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.db.models import AssistantSettings, Task
from coreman.runtime.worker import assistant_shift
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import last_sent, say
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import build_ctx, seed_bot

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
FRIDAY_3PM = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # 北京周五 15:00
MONDAY_OPEN = datetime(2026, 11, 16, 1, 30, tzinfo=UTC)  # 北京周一 09:30
FRIDAY_CLOSE = datetime(2026, 11, 13, 11, 0, tzinfo=UTC)  # 北京周五 19:00


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    monkeypatch.setattr(assistant_shift, "utcnow", lambda: FRIDAY_3PM)


async def prefs_of(session: AsyncSession, bot: Any) -> AssistantSettings:
    row = await session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    return row


async def test_the_owner_clocks_out_without_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    done, relay = await say(db_engine, db_session, bot, "下班")
    assert done.status == "succeeded" and done.result == {"assistant": "shift_off"}
    assert relay.requests == []
    prefs = await prefs_of(db_session, bot)
    assert prefs.shift_override == "off" and prefs.shift_override_until == MONDAY_OPEN
    # 说「下班」之前在班：记下今天上过班，下班编译凭它触发。
    assert prefs.worked_on == settings.local_today(prefs, FRIDAY_3PM)
    assert not settings.on_shift(prefs, FRIDAY_3PM)
    # 到点自动失效：周一 09:30 起按工作时段上班。
    assert not settings.on_shift(prefs, MONDAY_OPEN - timedelta(seconds=1))
    assert settings.on_shift(prefs, MONDAY_OPEN)
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.target == {"chat_id": "oc_private"}
    assert "已下班" in sent.payload["markdown"]
    assert "11 月 16 日（周一）09:30" in sent.payload["markdown"]


async def test_the_owner_clocks_in_after_clocking_out(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    # 按工作时段：「上班」到今天的下班时间为止。
    (await settings.get(db_session, bot.id)).always_on = False
    await db_session.commit()
    await say(db_engine, db_session, bot, "/下班")
    done, relay = await say(db_engine, db_session, bot, " 上班 ")
    assert done.result == {"assistant": "shift_on"} and relay.requests == []
    prefs = await prefs_of(db_session, bot)
    assert prefs.shift_override == "on" and prefs.shift_override_until == FRIDAY_CLOSE
    assert settings.on_shift(prefs, FRIDAY_3PM)
    assert not settings.on_shift(prefs, FRIDAY_CLOSE)
    sent = await last_sent(db_session, bot)
    assert sent is not None and "已上班" in sent.payload["markdown"]
    assert "今天 19:00" in sent.payload["markdown"]
    # 冷启动还没做完：回复里说明。
    assert "还没开始看消息" in sent.payload["markdown"]


async def test_around_the_clock_clocking_in_only_cancels_clocking_out(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """全天候（默认）：「下班」照样到下一个上班时刻；「上班」只是取消「下班」，不说到几点。"""
    bot, _, _ = await active_assistant(db_session)
    await say(db_engine, db_session, bot, "下班")
    prefs = await prefs_of(db_session, bot)
    assert prefs.always_on is True
    assert prefs.shift_override == "off" and prefs.shift_override_until == MONDAY_OPEN
    assert not settings.on_shift(prefs, FRIDAY_3PM)
    # 到点之后恢复全天候：周一 09:30 之前的周末也不在班，之后一直在班。
    assert settings.on_shift(prefs, MONDAY_OPEN)
    done, relay = await say(db_engine, db_session, bot, "上班")
    assert done.result == {"assistant": "shift_on"} and relay.requests == []
    prefs = await prefs_of(db_session, bot)
    assert prefs.shift_override is None and prefs.shift_override_until is None
    assert settings.on_shift(prefs, FRIDAY_CLOSE + timedelta(hours=5))
    sent = await last_sent(db_session, bot)
    assert sent is not None
    text = sent.payload["markdown"]
    assert text.startswith("已上班，全天候看你的飞书消息。")
    assert "为止" not in text and "19:00" not in text


async def test_clocking_out_keeps_a_day_still_owed_its_compile(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """全天候、前一天还欠着整理（运行时暂停没补上）：说「下班」不把它覆盖成今天；整理过了才记今天。"""
    bot, _, _ = await active_assistant(db_session)
    prefs = await settings.get(db_session, bot.id)
    owed = settings.local_today(prefs, FRIDAY_3PM) - timedelta(days=1)
    prefs.worked_on = owed
    await db_session.commit()
    await say(db_engine, db_session, bot, "下班")
    prefs = await prefs_of(db_session, bot)
    assert prefs.shift_override == "off" and prefs.worked_on == owed
    prefs.compiled_on = owed
    await db_session.commit()
    await say(db_engine, db_session, bot, "上班")
    prefs = await prefs_of(db_session, bot)
    assert prefs.worked_on == settings.local_today(prefs, FRIDAY_3PM)


async def test_someone_else_cannot_switch_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    done, relay = await say(db_engine, db_session, bot, "下班", sender="lisi", chat_id="oc_lisi")
    assert done.status == "succeeded" and done.result == {"assistant": "shift_not_owner"}
    assert relay.requests == []
    assert await db_session.get(AssistantSettings, bot.id, populate_existing=True) is None
    assert await last_sent(db_session, bot) is None


async def test_the_command_in_a_group_is_an_ordinary_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    task = await chat_task(
        db_session, bot, "下班", sender="owner_pid", chat_type="group", chat_id="oc_group"
    )
    await run(db_engine, task, FakeRelay("normal"))
    done = await db_session.get(Task, task.id, populate_existing=True)
    assert done is not None and (done.result or {}).get("assistant") is None
    # 群里的一轮照常开轮（会建出设置行），但没有任何临时切换。
    prefs = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert prefs is None or (prefs.shift_override is None and prefs.worked_on is None)


async def test_it_is_not_a_command_for_ordinary_bots(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await chat_task(db_session, bot, "下班")
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert len(relay.requests) == 1


async def test_a_sentence_containing_the_word_goes_to_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    done, relay = await say(db_engine, db_session, bot, "我今天几点下班比较合适")
    assert (done.result or {}).get("assistant") is None and len(relay.requests) == 1


async def test_an_assistant_that_is_being_stopped_does_not_switch(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "上班")
    assert done.result == {"assistant": "shift_unavailable"} and relay.requests == []
    assert await db_session.get(AssistantSettings, bot.id, populate_existing=True) is None


async def test_a_queued_platform_turn_is_left_alone(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """命令在入站阶段就结束了，不开轮：同会话排着的平台来源任务不被取消，也不被合并。"""
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    # 先让命令这一轮被认领，再排平台来源的任务：`chat_task` 认领的是队列里最早的一条。
    command = await chat_task(
        db_session, bot, "下班", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    waiting = await platform_turns.enqueue(
        db_session, bot, HANDOVER_KIND, {"items": []}, dedupe_key="handover:demo"
    )
    await db_session.commit()
    assert waiting is not None
    relay = FakeRelay("normal")
    await run(db_engine, command, relay)
    done = await db_session.get(Task, command.id, populate_existing=True)
    assert done is not None
    assert done.result == {"assistant": "shift_off"} and relay.requests == []
    left = await db_session.get(Task, waiting.id, populate_existing=True)
    assert left is not None and left.status == "queued" and left.cancel_requested_at is None


async def test_a_queued_command_is_not_merged_into_the_running_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    kw: dict[str, Any] = {"sender": "owner_pid", "chat_type": "single", "chat_id": "oc_private"}
    head = await chat_task(db_session, bot, "帮我看看今天的消息", **kw)
    head.status = "running"
    await db_session.commit()
    follow = await chat_task(db_session, bot, "顺便看下日程", **kw)
    follow.status = "running"
    await db_session.commit()
    command = await chat_task(db_session, bot, "下班", **kw)
    for row in (follow, command):
        row.status, row.claimed_by = "queued", None
    await db_session.commit()
    relay = FakeRelay("normal")
    await run(db_engine, head, relay)
    merged = await db_session.get(Task, follow.id, populate_existing=True)
    assert merged is not None and merged.error_code == "merged"
    left = await db_session.get(Task, command.id, populate_existing=True)
    assert left is not None and left.status == "queued" and left.error_code is None
    assert "下班" not in str(relay.requests)
    # 轮到它自己那一轮时照常截下。
    left.status = "running"
    await db_session.commit()
    again = FakeRelay("normal")
    await ChatTaskHandler().run(
        build_ctx(db_engine, left, relay_client_factory=lambda _r: again.client())
    )
    done = await db_session.get(Task, command.id, populate_existing=True)
    assert done is not None and done.result == {"assistant": "shift_off"}
    assert again.requests == []


def test_when_describes_the_moment_in_the_owners_timezone() -> None:
    prefs = AssistantSettings(
        timezone="Asia/Shanghai", work_start="09:30", work_end="19:00", workdays=[1, 2, 3, 4, 5]
    )
    assert assistant_shift.when(prefs, FRIDAY_CLOSE, FRIDAY_3PM) == "今天 19:00"
    midnight = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)
    assert assistant_shift.when(prefs, midnight, FRIDAY_3PM) == "明天 00:00"
    assert assistant_shift.when(prefs, MONDAY_OPEN, FRIDAY_3PM) == "11 月 16 日（周一）09:30"
