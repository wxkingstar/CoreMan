"""免打扰时段与摘要时点：按所有者时区算，免打扰可以跨午夜，结束的那一刻本身是一个时点。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from coreman.core.assistants import settings
from coreman.core.db.models import AssistantSettings


def prefs(**values: object) -> AssistantSettings:
    base: dict[str, object] = {
        "timezone": "Asia/Shanghai",
        "digest_times": ["09:00", "13:30", "18:00"],
        "quiet_enabled": True,
        "quiet_start": "22:00",
        "quiet_end": "08:00",
    }
    return AssistantSettings(**{**base, **values})


def local(hour: int, minute: int = 0, day: int = 16) -> datetime:
    """所有者当地某天的钟点。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_quiet_hours_cross_midnight() -> None:
    row = prefs()
    assert settings.quiet(row, local(22))
    assert settings.quiet(row, local(3))
    assert settings.quiet(row, local(7, 59))
    assert not settings.quiet(row, local(8))
    assert not settings.quiet(row, local(21, 59))
    assert not settings.quiet(prefs(quiet_enabled=False), local(23))
    # 不跨午夜的时段同样认。
    noon = prefs(quiet_start="12:00", quiet_end="14:00")
    assert settings.quiet(noon, local(13)) and not settings.quiet(noon, local(14))


def test_slots_skip_quiet_hours_and_add_their_end() -> None:
    assert settings.digest_slots(prefs()) == ["08:00", "09:00", "13:30", "18:00"]
    late = prefs(digest_times=["07:00", "23:30", "12:00"])
    assert settings.digest_slots(late) == ["08:00", "12:00"]
    assert settings.digest_slots(prefs(quiet_enabled=False)) == ["09:00", "13:30", "18:00"]


def test_next_digest_is_the_next_slot_after_now() -> None:
    row = prefs()
    assert settings.next_digest(row, local(10)) == local(13, 30)
    # 正好在时点上：下一个。
    assert settings.next_digest(row, local(13, 30)) == local(18)
    # 晚上：跳过免打扰，第二天免打扰结束时。
    assert settings.next_digest(row, local(19)) == local(8, day=17)
    assert settings.next_digest(row, local(2, day=17)) == local(8, day=17)
