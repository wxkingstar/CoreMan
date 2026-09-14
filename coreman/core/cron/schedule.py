"""五字段 cron，按本地墙钟解释、以 UTC 持久化。

夏令时跳过不存在的时间；重复墙钟时间只取第一次，避免同一业务时刻执行两次。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

from coreman.core.timeutils import aware_utc


def next_run(expression: str, timezone: str, after: datetime) -> datetime:
    if (
        len(expression) > 128
        or len(expression.split()) != 5
        or re.search(r"(?<![A-Z])[RH](?![A-Z])", expression.upper())
    ):
        raise ValueError("请使用五字段 cron 表达式（分 时 日 月 周）")
    try:
        zone = ZoneInfo(timezone)
        after = aware_utc(after)
        local = after.astimezone(zone).replace(tzinfo=None)
        it = croniter(expression, local, max_years_between_matches=8)
        for _ in range(4096):
            wall = it.get_next(datetime)
            candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate > after and candidate.astimezone(zone).replace(tzinfo=None) == wall:
                return candidate
    except (
        ZoneInfoNotFoundError,
        CroniterBadDateError,
        CroniterBadCronError,
        OverflowError,
    ) as exc:
        raise ValueError("cron 表达式或时区无效，或八年内没有触发时间") from exc
    raise ValueError("cron 在指定时区没有可用触发时间")
