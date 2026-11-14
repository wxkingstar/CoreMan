"""助理设置里的时间判断：所有者时区的自然日、是否在工作时段、「上班」「下班」的临时切换。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from coreman.core.assistants import settings
from coreman.core.db.models import AssistantSettings


def row(**over: Any) -> AssistantSettings:
    values: dict[str, Any] = {
        "bot_id": uuid.uuid4(),
        "open_id": "ou_owner",
        "tenant_key": "tenant-example",
        "platform_user_id": "owner_pid",
        "chat_id": "oc_private",
        "timezone": "Asia/Shanghai",
        # 库里默认全天候；这里的用例看的是按工作时段的计算，全天候的单独写。
        "always_on": False,
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
    }
    return AssistantSettings(**{**values, **over})


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026 年 11 月某天的 UTC 时刻；13 日是周五，14 日是周六。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_identified_needs_all_four() -> None:
    assert settings.identified(row())
    for column in ("open_id", "tenant_key", "platform_user_id", "chat_id"):
        assert not settings.identified(row(**{column: ""})), column


def test_local_today_follows_the_owners_timezone() -> None:
    late = at(13, 16, 30)  # 北京时间 14 日 00:30，纽约 13 日 11:30
    assert settings.local_today(row(), late) == date(2026, 11, 14)
    assert settings.local_today(row(timezone="America/New_York"), late) == date(2026, 11, 13)
    # 不带时区的时刻按 UTC。
    assert settings.local_today(row(), late.replace(tzinfo=None)) == date(2026, 11, 14)


def test_an_unknown_timezone_falls_back_to_the_default() -> None:
    for name in ("Mars/Olympus", "", "../etc/passwd"):
        assert settings.zone(row(timezone=name)).key == settings.DEFAULT_TIMEZONE, name


def test_on_shift_within_working_hours_on_workdays() -> None:
    s = row()
    assert settings.on_shift(s, at(13, 1, 30))  # 周五 09:30
    assert not settings.on_shift(s, at(13, 1, 29))  # 周五 09:29
    assert settings.on_shift(s, at(13, 10, 59))  # 周五 18:59
    assert not settings.on_shift(s, at(13, 11, 0))  # 周五 19:00
    assert not settings.on_shift(s, at(14, 3, 0))  # 周六 11:00
    assert settings.on_shift(row(workdays=[6]), at(14, 3, 0))


def test_workday_is_judged_in_the_owners_timezone() -> None:
    s = row(timezone="America/New_York")
    assert settings.on_shift(s, at(13, 15, 0))  # 纽约周五 10:00
    assert not settings.on_shift(s, at(14, 1, 0))  # 纽约周五 20:00（UTC 已是周六）
    assert not settings.on_shift(s, at(14, 15, 0))  # 纽约周六 10:00


def test_always_on_is_on_shift_at_any_hour() -> None:
    s = row(always_on=True)
    assert settings.on_shift(s, at(13, 1, 29))  # 周五 09:29
    assert settings.on_shift(s, at(13, 16, 0))  # 周六 00:00
    assert settings.on_shift(row(always_on=True, workdays=[6]), at(16, 3, 0))  # 周一


def test_a_switch_still_wins_over_always_on() -> None:
    work = at(13, 3, 0)
    off = row(always_on=True, shift_override="off", shift_override_until=work + timedelta(hours=1))
    assert not settings.on_shift(off, work)
    # 过期后恢复全天候，不看钟点。
    assert settings.on_shift(off, at(14, 3, 0))


def test_override_wins_until_it_expires() -> None:
    work, weekend = at(13, 3, 0), at(14, 3, 0)
    off = row(shift_override="off", shift_override_until=work + timedelta(hours=1))
    assert not settings.on_shift(off, work)
    assert settings.on_shift(off, work + timedelta(hours=2))  # 过期后按工作时段
    on = row(shift_override="on", shift_override_until=weekend + timedelta(hours=1))
    assert settings.on_shift(on, weekend)
    assert not settings.on_shift(on, weekend + timedelta(hours=2))
    # 没有到期时刻的切换一直有效。
    assert settings.on_shift(row(shift_override="on"), weekend)
    assert not settings.on_shift(row(shift_override="off"), work)
