"""工作时段的时刻计算：「上班」「下班」到什么时候为止，跨时区、跨周末，到点自动失效。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from coreman.core.assistants import settings
from coreman.core.db.models import AssistantSettings


def row(**over: Any) -> AssistantSettings:
    values: dict[str, Any] = {
        "bot_id": uuid.uuid4(),
        "timezone": "Asia/Shanghai",
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
    }
    return AssistantSettings(**{**values, **over})


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026 年 11 月某天的 UTC 时刻；13 日是周五，14 日周六，16 日周一。北京时间 = UTC+8。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_end_today_only_on_a_workday_before_closing() -> None:
    assert settings.end_today(row(), at(13, 2)) == at(13, 11)  # 周五 10:00 → 19:00
    assert settings.end_today(row(), at(13, 0)) == at(13, 11)  # 周五 08:00，还没上班也算
    assert settings.end_today(row(), at(13, 11)) is None  # 周五 19:00 整
    assert settings.end_today(row(), at(14, 3)) is None  # 周六


def test_next_start_skips_the_weekend() -> None:
    assert settings.next_start(row(), at(13, 0)) == at(13, 1, 30)  # 周五 08:00 → 当天 09:30
    assert settings.next_start(row(), at(13, 1, 30)) == at(16, 1, 30)  # 09:30 整 → 下周一
    assert settings.next_start(row(), at(13, 11, 30)) == at(16, 1, 30)  # 周五 19:30 → 周一
    assert settings.next_start(row(), at(14, 3)) == at(16, 1, 30)  # 周六 → 周一
    assert settings.next_start(row(workdays=[]), at(13, 0)) is None


def test_times_follow_the_owners_timezone() -> None:
    owner = row(timezone="America/New_York")  # 11 月是 UTC-5
    noon = at(13, 16)  # 纽约周五 11:00
    assert settings.end_today(owner, noon) == at(14, 0)  # 纽约周五 19:00
    assert settings.next_start(owner, noon) == at(16, 14, 30)  # 纽约周一 09:30
    # UTC 已经是周六凌晨，纽约还是周五晚上：按所有者的星期算。
    late = at(14, 3)  # 纽约周五 22:00
    assert settings.end_today(owner, late) is None
    assert settings.next_midnight(owner, late) == at(14, 5)  # 纽约周六 00:00
    assert settings.next_midnight(row(), at(13, 13)) == at(13, 16)  # 北京周六 00:00


def test_clock_in_lasts_until_closing_time_on_a_workday() -> None:
    owner = row()
    until = settings.clock_in(owner, at(13, 0))  # 周五 08:00，还没到上班时间
    assert until == at(13, 11) and owner.shift_override == "on"
    assert owner.shift_override_until == until
    assert settings.on_shift(owner, at(13, 0, 1))
    assert settings.override(owner, at(13, 10, 59)) == "on"
    # 到点自动失效：之后按工作时段算（19:00 已下班）。
    assert settings.override(owner, at(13, 11)) is None
    assert not settings.on_shift(owner, at(13, 11))


def test_clock_in_outside_working_days_lasts_until_local_midnight() -> None:
    owner = row()
    until = settings.clock_in(owner, at(14, 3))  # 周六 11:00
    assert until == at(14, 16)  # 北京周日 00:00
    assert settings.on_shift(owner, at(14, 15, 59))
    assert not settings.on_shift(owner, at(14, 16))
    assert settings.override(owner, at(14, 16)) is None
    # 周五下班之后说上班：到当晚零点。
    assert settings.clock_in(row(), at(13, 13)) == at(13, 16)


def test_clock_out_lasts_until_the_next_shift_across_the_weekend() -> None:
    owner = row()
    until = settings.clock_out(owner, at(13, 7))  # 周五 15:00
    assert until == at(16, 1, 30) and owner.shift_override == "off"
    assert not settings.on_shift(owner, at(13, 8))  # 本来在工作时段里
    assert not settings.on_shift(owner, at(14, 3))
    assert not settings.on_shift(owner, at(16, 1, 29))
    # 周一 09:30 临时切换过期，按工作时段自动上班。
    assert settings.on_shift(owner, at(16, 1, 30))
    assert settings.override(owner, at(16, 1, 30)) is None


def test_clock_out_before_opening_ends_at_opening() -> None:
    owner = row()
    assert settings.clock_out(owner, at(13, 0)) == at(13, 1, 30)  # 周五 08:00 → 当天 09:30
    assert settings.on_shift(owner, at(13, 1, 30))


def test_clock_out_in_another_timezone() -> None:
    owner = row(timezone="America/New_York")
    assert settings.clock_out(owner, at(13, 16)) == at(16, 14, 30)


def test_clock_out_without_working_days_lasts_a_day() -> None:
    owner = row(workdays=[])
    now = at(13, 7)
    assert settings.clock_out(owner, now) == now + timedelta(days=1)


def test_clear_override() -> None:
    owner = row()
    settings.clock_out(owner, at(13, 7))
    settings.clear_override(owner)
    assert owner.shift_override is None and owner.shift_override_until is None
    assert settings.on_shift(owner, at(13, 8))
