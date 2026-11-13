"""所有者时区里「今天」的起止：预算与换新会话都按它。"""

from __future__ import annotations

from datetime import UTC, date, datetime

from coreman.core.assistants import delivery


def test_a_day_in_shanghai_starts_at_sixteen_utc_the_day_before() -> None:
    start, end = delivery.day_bounds(delivery.zone_named("Asia/Shanghai"), date(2026, 11, 13))
    assert start == datetime(2026, 11, 12, 16, tzinfo=UTC)
    assert end == datetime(2026, 11, 13, 16, tzinfo=UTC)


def test_the_day_clocks_go_back_is_twenty_five_hours() -> None:
    start, end = delivery.day_bounds(delivery.zone_named("America/New_York"), date(2026, 11, 1))
    assert start == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 5, tzinfo=UTC)


def test_an_unknown_zone_falls_back_to_the_default() -> None:
    assert delivery.zone_named("Mars/Olympus").key == "Asia/Shanghai"
    assert delivery.zone_named(None).key == "Asia/Shanghai"
