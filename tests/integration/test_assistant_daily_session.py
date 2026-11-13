"""助理的主会话按所有者时区的自然日换新；子任务会话、续接轮次、普通员工不受影响。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings
from coreman.core.chat import sessions
from coreman.core.db.models import Bot
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.approval_resume import ApprovalResumeHandler
from coreman.runtime.worker.assistant_handover import AssistantHandoverHandler
from coreman.runtime.worker.chat import assistant_day
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.credential_resume import CredentialResumeHandler
from tests.integration.assistant_helpers import seed_assistant
from tests.integration.credential_helpers import owner
from tests.integration.test_assistant_notes_injection import (
    MARK,
    TONE,
    assistant_chat,
    hand_back,
    next_turn,
    run,
)
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")
MAIN = "oc_private"
DAY = "SELECT local_day FROM chat_sessions WHERE bot_id = :bot AND session_key = :key"


async def day_of(session: AsyncSession, bot: Bot, key: str = MAIN) -> Any:
    return (await session.execute(text(DAY), {"bot": bot.id, "key": key})).scalar_one()


async def turn(
    session: AsyncSession, bot: Bot, now: datetime, key: str = MAIN
) -> sessions.SessionInfo:
    """开一轮时会话这一步：取（或建）会话，再过换新。"""
    info = await sessions.get_or_create(
        session,
        bot_id=bot.id,
        session_key=key,
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    out = await assistant_day.roll(
        session,
        bot,
        info,
        session_key=key,
        chat_id=MAIN,
        backend="claude",
        speaker_user_id=None,
        now=now,
    )
    await session.commit()
    return out


async def test_the_next_days_first_turn_starts_a_new_session_with_the_pinned_pages_again(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, task = await assistant_chat(db_session)
    first = await run(db_engine, db_session, task)
    today = settings.local_today(await settings.get(db_session, bot.id), utcnow())
    assert await day_of(db_session, bot) == today
    # 当天的下一轮续上同一个会话。
    await hand_back(db_session, bot)
    second = await run(db_engine, db_session, await next_turn(db_session, bot, "再看一下"))
    assert second["session_id"] == first["session_id"]
    assert MARK not in turn_block(second)
    # 这个会话是昨天开始的：今天的第一轮换新会话，置顶页与索引重新给一次。
    await hand_back(db_session, bot)
    await db_session.execute(text("UPDATE chat_sessions SET local_day = local_day - 1"))
    await db_session.commit()
    third = await run(db_engine, db_session, await next_turn(db_session, bot, "早"))
    assert third["session_id"] != first["session_id"]
    assert MARK in turn_block(third) and TONE in turn_block(third)
    assert await day_of(db_session, bot) == today
    # 换新之后当天接着续。
    await hand_back(db_session, bot)
    fourth = await run(db_engine, db_session, await next_turn(db_session, bot, "继续"))
    assert fourth["session_id"] == third["session_id"]


@pytest.mark.parametrize(
    ("zone", "start", "same_day", "next_day", "days"),
    [
        (
            "Asia/Shanghai",
            datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
            datetime(2026, 11, 12, 15, 59, tzinfo=UTC),
            datetime(2026, 11, 12, 16, 0, tzinfo=UTC),
            (date(2026, 11, 12), date(2026, 11, 13)),
        ),
        (
            "America/New_York",
            datetime(2026, 11, 12, 20, 0, tzinfo=UTC),
            datetime(2026, 11, 13, 4, 59, tzinfo=UTC),
            datetime(2026, 11, 13, 5, 0, tzinfo=UTC),
            (date(2026, 11, 12), date(2026, 11, 13)),
        ),
    ],
)
async def test_the_day_changes_at_the_owners_midnight(
    db_session: AsyncSession,
    zone: str,
    start: datetime,
    same_day: datetime,
    next_day: datetime,
    days: tuple[date, date],
) -> None:
    bot, _ = await seed_assistant(db_session)
    (await settings.get(db_session, bot.id)).timezone = zone
    await db_session.commit()
    first = await turn(db_session, bot, start)
    assert first.is_new and await day_of(db_session, bot) == days[0]
    kept = await turn(db_session, bot, same_day)
    assert not kept.is_new and kept.relay_session_id == first.relay_session_id
    fresh = await turn(db_session, bot, next_day)
    assert fresh.is_new and fresh.relay_session_id != first.relay_session_id
    assert fresh.inherited_from is None and await day_of(db_session, bot) == days[1]
    again = await turn(db_session, bot, next_day)
    assert not again.is_new and again.relay_session_id == fresh.relay_session_id


async def test_job_sessions_are_left_alone(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    key = f"{MAIN}#job:7"
    first = await turn(db_session, bot, datetime(2026, 11, 12, 10, tzinfo=UTC), key)
    later = await turn(db_session, bot, datetime(2026, 11, 20, 10, tzinfo=UTC), key)
    assert later.relay_session_id == first.relay_session_id and not later.is_new
    assert await day_of(db_session, bot, key) is None


async def test_a_session_from_before_the_upgrade_is_stamped_not_replaced(
    db_session: AsyncSession,
) -> None:
    bot, _ = await seed_assistant(db_session)
    first = await turn(db_session, bot, datetime(2026, 11, 12, 10, tzinfo=UTC))
    await db_session.execute(text("UPDATE chat_sessions SET local_day = NULL"))
    await db_session.commit()
    kept = await turn(db_session, bot, datetime(2026, 11, 20, 10, tzinfo=UTC))
    assert kept.relay_session_id == first.relay_session_id
    assert await day_of(db_session, bot) == date(2026, 11, 20)


def test_only_resume_turns_opt_out() -> None:
    assert ChatTaskHandler.DAILY_SESSIONS is True
    assert AssistantHandoverHandler.DAILY_SESSIONS is True
    assert ApprovalResumeHandler.DAILY_SESSIONS is False
    assert CredentialResumeHandler.DAILY_SESSIONS is False


async def test_standard_employees_never_get_a_day(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, task, _ = await owner(db_session)
    await run(db_engine, db_session, task)
    assert await day_of(db_session, bot) is None
