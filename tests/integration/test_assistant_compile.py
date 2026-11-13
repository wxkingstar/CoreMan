"""下班编译：下班后才触发、每个自然日至多一次、没有活动不入队；文本里没有内容。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import compiling, followups, platform_turns, settings
from coreman.core.assistants.kinds import COMPILE_KIND
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.assistant_compile import AssistantCompileHandler
from tests.integration.assistant_helpers import SECRET
from tests.integration.shift_helpers import tasks_of, working
from tests.integration.test_assistant_platform_turn_handler import run
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


async def noted(session: AsyncSession, bot: Bot) -> None:
    """今天有一件事：记了一条待跟进（标题是内容，不该出现在任务里）。"""
    await followups.add(session, bot.id, category="remind", title=f"周五交报价 {SECRET}")
    await session.commit()


async def after_work(session: AsyncSession, bot: Bot, prefs: AssistantSettings) -> datetime:
    """上过班、有活动、现在下班了；返回下班后的一个时刻。"""
    assert await compiling.step(session, bot, prefs, utcnow()) == "worked"
    await noted(session, bot)
    prefs.shift_override = "off"
    await session.commit()
    return utcnow() + timedelta(seconds=1)


async def test_being_on_shift_only_marks_the_day(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session)
    await noted(db_session, bot)
    now = utcnow()
    assert await compiling.step(db_session, bot, prefs, now) == "worked"
    assert prefs.worked_on == settings.local_today(prefs, now) and prefs.compiled_on is None
    # 在班期间不编译，也不重复写同一天。
    assert await compiling.step(db_session, bot, prefs, now) is None
    await db_session.commit()
    assert await tasks_of(db_session, bot, COMPILE_KIND) == []


async def test_after_the_shift_one_turn_is_queued_once_a_day(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session)
    later = await after_work(db_session, bot, prefs)
    day = prefs.worked_on
    assert day is not None
    assert await compiling.step(db_session, bot, prefs, later) == "queued"
    assert prefs.compiled_on == day
    await db_session.commit()
    (task,) = await tasks_of(db_session, bot, COMPILE_KIND)
    assert task.dedupe_key == f"assistant-compile:{bot.id}:{day.isoformat()}"
    assert task.session_key == prefs.chat_id and task.inbound_event_id is None
    assert task.payload["day"] == day.isoformat()
    # 载荷里只有日期与时刻。
    assert SECRET not in json.dumps(task.payload, ensure_ascii=False)
    # 当天再上班、再下班：不再编译。
    prefs.shift_override = "on"
    assert await compiling.step(db_session, bot, prefs, later) is None
    await noted(db_session, bot)
    prefs.shift_override = "off"
    assert await compiling.step(db_session, bot, prefs, later) is None
    await db_session.commit()
    assert len(await tasks_of(db_session, bot, COMPILE_KIND)) == 1


async def test_a_day_without_activity_is_not_compiled(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session)
    assert await compiling.step(db_session, bot, prefs, utcnow()) == "worked"
    prefs.shift_override = "off"
    later = utcnow() + timedelta(seconds=1)
    assert await compiling.step(db_session, bot, prefs, later) == "idle"
    # 这一天不再看：之后的每次循环都不再查活动。
    assert prefs.compiled_on == prefs.worked_on and prefs.compiled_at is None
    assert await compiling.step(db_session, bot, prefs, later) is None
    await db_session.commit()
    assert await tasks_of(db_session, bot, COMPILE_KIND) == []


async def test_nothing_happens_without_a_shift_today(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session, shift="off")
    await noted(db_session, bot)
    assert await compiling.step(db_session, bot, prefs, utcnow()) is None
    assert prefs.worked_on is None and prefs.compiled_on is None


async def test_nothing_happens_before_the_cold_start_is_done(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session)
    prefs.cold_start_done_at = None
    await noted(db_session, bot)
    assert await compiling.step(db_session, bot, prefs, utcnow()) is None
    assert prefs.worked_on is None


async def test_a_manual_clock_out_triggers_it(db_session: AsyncSession) -> None:
    bot, _, prefs = await working(db_session, shift=None)
    now = utcnow()
    settings.clock_in(prefs, now)
    assert await compiling.step(db_session, bot, prefs, now) == "worked"
    await noted(db_session, bot)
    settings.clock_out(prefs, now)
    assert await compiling.step(db_session, bot, prefs, now + timedelta(seconds=1)) == "queued"


async def test_a_paused_runtime_postpones_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _, prefs = await working(db_session)
    later = await after_work(db_session, bot, prefs)

    async def blocked(*_args: object) -> str:
        return "node_offline"

    monkeypatch.setattr(compiling.runtime, "turn_blocker", blocked)
    assert await compiling.step(db_session, bot, prefs, later) == "paused"
    # 没有记成处理过：恢复后再入队。
    assert prefs.compiled_on is None
    monkeypatch.undo()
    assert await compiling.step(db_session, bot, prefs, later) == "queued"


async def test_an_exhausted_budget_skips_the_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _, prefs = await working(db_session)
    later = await after_work(db_session, bot, prefs)

    async def spent(*_args: object) -> int:
        return 0

    monkeypatch.setattr(compiling.delivery, "budget_left", spent)
    assert await compiling.step(db_session, bot, prefs, later) == "budget"
    assert prefs.compiled_on == prefs.worked_on and prefs.compiled_at is None
    await db_session.commit()
    assert await tasks_of(db_session, bot, COMPILE_KIND) == []


async def test_tick_commits_per_assistant_and_skips_inactive_records(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, prefs = await working(db_session)
    factory = make_session_factory(db_engine)
    assert await compiling.tick(factory, utcnow()) == {"worked": 1}
    await noted(db_session, bot)
    prefs = await settings.get(db_session, bot.id)
    await db_session.refresh(prefs)
    prefs.shift_override = "off"
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    later = utcnow() + timedelta(seconds=1)
    assert await compiling.tick(factory, later) == {}
    bot.assistant_state = "active"
    await db_session.commit()
    assert await compiling.tick(factory, later) == {"queued": 1}
    assert await compiling.tick(factory, later) == {}
    assert len(await tasks_of(db_session, bot, COMPILE_KIND)) == 1


async def test_the_turn_gives_ids_not_content(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, prefs = await working(db_session)
    later = await after_work(db_session, bot, prefs)
    assert await compiling.step(db_session, bot, prefs, later) == "queued"
    await db_session.commit()
    task = await claim(db_session)
    assert task.kind == COMPILE_KIND
    done, relay = await run(db_engine, db_session, task, AssistantCompileHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert "下班整理" in text and "F001 · remind · 新增" in text
    assert SECRET not in text
    stored = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert stored is not None
    # 成功之后才推进活动窗口；编译这一轮自己不算下一次的活动。
    assert stored.compiled_at == datetime.fromisoformat(task.payload["until"])
    found = await compiling.activity(db_session, bot.id, stored.chat_id, stored.compiled_at)
    assert found.empty


async def test_a_turn_with_nothing_left_to_compile_is_cancelled(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await working(db_session)
    future = (utcnow() + timedelta(hours=1)).isoformat()
    queued = await platform_turns.enqueue(
        db_session,
        bot,
        COMPILE_KIND,
        {"day": "2026-11-13", "since": future, "until": future},
        dedupe_key="assistant-compile:demo",
    )
    await db_session.commit()
    assert queued is not None
    task = await claim(db_session)
    done, relay = await run(db_engine, db_session, task, AssistantCompileHandler())
    assert done.status == "cancelled" and done.error_code == "platform_turn_inactive"
    assert relay.requests == []
    stored = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert stored is not None and stored.compiled_at is None
