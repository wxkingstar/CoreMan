"""跨 API 和运行时一致处理历史时间戳。"""

from datetime import UTC, datetime
from typing import Any


def aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(UTC)
