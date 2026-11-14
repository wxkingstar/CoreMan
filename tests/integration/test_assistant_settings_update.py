"""设置的读出与修改：每条校验、名单整份替换且在库里是密文、改时段会清掉临时切换。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed, settings
from coreman.core.db.models import Bot, User
from tests.integration.assistant_helpers import raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
NOW = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # 北京周五 15:00
# 会话 id 里只有字母、数字、下划线与连字符：断言「库里不含明文」用这一段认得出来的串。
SECRET_ID = "s3cr3tchat7f3a"
STORED = (
    "SELECT coalesce(focus_chats_enc::text, '') || coalesce(ignored_chats_enc::text, '')"
    " FROM assistant_settings WHERE bot_id = :id"
)


async def keyed(session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot, owner


async def test_view_of_a_fresh_assistant(db_session: AsyncSession) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    shown = settings.view(await settings.get(db_session, bot_id), NOW)
    assert shown == {
        "timezone": "Asia/Shanghai",
        "always_on": True,
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
        "focus_chats": [],
        "ignored_chats": [],
        "on_shift": True,
        "shift_override": None,
        "shift_override_until": None,
        "cold_start": "pending",
        "today": "2026-11-13",
    }


async def test_update_changes_only_what_is_given(db_session: AsyncSession) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    row = await settings.update(
        db_session, bot_id, now=NOW, timezone="America/New_York", workdays=[5, 1, 3]
    )
    await db_session.commit()
    shown = settings.view(row, NOW)
    assert shown["timezone"] == "America/New_York" and shown["workdays"] == [1, 3, 5]
    assert (shown["work_start"], shown["work_end"]) == ("09:30", "19:00")
    # 全天候没动：纽约此刻是周五凌晨 02:00，照样在班。
    assert shown["always_on"] is True and shown["on_shift"] is True
    row = await settings.update(db_session, bot_id, now=NOW, always_on=False)
    shown = settings.view(row, NOW)
    # 改成按工作时段：凌晨 02:00 不在班；时区、工作日不动。
    assert shown["always_on"] is False and shown["on_shift"] is False
    assert shown["timezone"] == "America/New_York" and shown["today"] == "2026-11-13"
    row = await settings.update(db_session, bot_id, now=NOW, work_start="08:00")
    assert (row.work_start, row.work_end) == ("08:00", "19:00")


async def test_chat_lists_are_replaced_whole_and_stored_encrypted(
    db_session: AsyncSession,
) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    await settings.update(
        db_session,
        bot_id,
        now=NOW,
        focus_chats=[f"oc_{SECRET_ID}", " oc_team ", f"oc_{SECRET_ID}"],
        ignored_chats=["oc_noise"],
    )
    await db_session.commit()
    assert SECRET_ID not in str(await raw(db_session, STORED, id=bot_id))
    db_session.expire_all()
    shown = settings.view(await settings.get(db_session, bot_id), NOW)
    # 去掉首尾空白与重复，保留顺序。
    assert shown["focus_chats"] == [f"oc_{SECRET_ID}", "oc_team"]
    assert shown["ignored_chats"] == ["oc_noise"]
    await settings.update(db_session, bot_id, now=NOW, focus_chats=[])
    await db_session.commit()
    db_session.expire_all()
    shown = settings.view(await settings.get(db_session, bot_id), NOW)
    assert shown["focus_chats"] == [] and shown["ignored_chats"] == ["oc_noise"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({}, "nothing_to_update"),
        ({"timezone": "Mars/Olympus"}, "invalid_timezone"),
        ({"timezone": ""}, "invalid_timezone"),
        ({"timezone": "../etc/passwd"}, "invalid_timezone"),
        ({"work_start": "9:30"}, "invalid_work_hours"),
        ({"work_end": "24:00"}, "invalid_work_hours"),
        ({"work_start": "19:00"}, "invalid_work_hours"),  # 与现有的下班时间相等
        ({"work_start": "20:00", "work_end": "08:00"}, "invalid_work_hours"),  # 不支持跨午夜
        ({"work_end": "09:00"}, "invalid_work_hours"),  # 早于现有的上班时间
        ({"workdays": []}, "invalid_workdays"),
        ({"workdays": [0, 1]}, "invalid_workdays"),
        ({"workdays": [1, 8]}, "invalid_workdays"),
        ({"workdays": [1, 1]}, "invalid_workdays"),
        ({"workdays": [True, 2]}, "invalid_workdays"),
        ({"focus_chats": ["om_message"]}, "invalid_chats"),
        ({"focus_chats": ["oc_a b"]}, "invalid_chats"),
        ({"ignored_chats": [f"oc_{'x' * 81}"]}, "invalid_chats"),
        ({"focus_chats": [f"oc_{n}" for n in range(6)]}, "too_many_chats"),
        ({"ignored_chats": [f"oc_{n}" for n in range(51)]}, "too_many_chats"),
        ({"focus_chats": ["oc_a"], "ignored_chats": ["oc_a"]}, "chat_in_both_lists"),
    ],
)
async def test_each_validation(
    db_session: AsyncSession, changes: dict[str, Any], code: str
) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    with pytest.raises(settings.SettingsError) as caught:
        await settings.update(db_session, bot_id, now=NOW, **changes)
    assert caught.value.code == code
    await db_session.rollback()
    # 被拒绝的修改什么都没写。
    shown = settings.view(await settings.get(db_session, bot_id), NOW)
    assert (shown["timezone"], shown["work_start"], shown["workdays"]) == (
        "Asia/Shanghai",
        "09:30",
        [1, 2, 3, 4, 5],
    )
    assert shown["focus_chats"] == [] and shown["ignored_chats"] == []


async def test_limits_are_reported_with_the_error(db_session: AsyncSession) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    with pytest.raises(settings.SettingsError) as caught:
        await settings.update(
            db_session, bot_id, now=NOW, focus_chats=[f"oc_{n}" for n in range(6)]
        )
    assert caught.value.payload() == {"error": "too_many_chats", "limit": settings.MAX_FOCUS}


async def test_a_chat_cannot_be_in_both_lists_across_two_updates(
    db_session: AsyncSession,
) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    await settings.update(db_session, bot_id, now=NOW, ignored_chats=["oc_a"])
    await db_session.commit()
    with pytest.raises(settings.SettingsError) as caught:
        await settings.update(db_session, bot_id, now=NOW, focus_chats=["oc_a"])
    assert caught.value.code == "chat_in_both_lists"


async def test_changing_the_schedule_clears_the_temporary_switch(
    db_session: AsyncSession,
) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    row = await settings.get(db_session, bot_id)
    settings.clock_out(row, NOW)
    await db_session.commit()
    # 只改名单：临时切换还在。
    row = await settings.update(db_session, bot_id, now=NOW, ignored_chats=["oc_noise"])
    assert settings.override(row, NOW) == "off"
    row = await settings.update(db_session, bot_id, now=NOW, work_end="20:00")
    assert row.shift_override is None and row.shift_override_until is None
    assert settings.view(row, NOW)["on_shift"] is True
    # 改全天候也清：它属于工作时段的设置。
    settings.clock_out(row, NOW)
    row = await settings.update(db_session, bot_id, now=NOW, always_on=True)
    assert settings.override(row, NOW) == "off"
    row = await settings.update(db_session, bot_id, now=NOW, always_on=False)
    assert row.shift_override is None and row.always_on is False


async def test_lists_cannot_be_written_without_the_data_key(db_session: AsyncSession) -> None:
    bot, owner = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    keys.forget()
    sealed.forget()
    with pytest.raises(settings.SettingsError) as caught:
        await settings.update(db_session, bot_id, now=NOW, focus_chats=["oc_a"])
    assert caught.value.code == "content_locked"
    await db_session.rollback()
    # 不碰名单的修改照常可用。
    row = await settings.update(db_session, bot_id, now=NOW, work_start="10:00")
    assert row.work_start == "10:00"


async def test_cold_start_state(db_session: AsyncSession) -> None:
    bot, _ = await keyed(db_session)
    bot_id = bot.id  # 回滚、expire_all 之后行上的属性已过期，先取出来
    row = await settings.get(db_session, bot_id)
    assert settings.cold_start_state(row) == "pending"
    row.cold_start_started_at = NOW
    assert settings.cold_start_state(row) == "in_progress"
    row.cold_start_done_at = NOW
    assert settings.cold_start_state(row) == "done"
