"""settings 表读写 + 进程内 60 秒缓存。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import Setting

_MISSING = object()


class SettingsStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > self._clock():
            return default if hit[1] is _MISSING else hit[1]
        async with self._factory() as session:
            row = (
                await session.execute(select(Setting).where(Setting.key == key))
            ).scalar_one_or_none()
        stored: Any = _MISSING if row is None else row.value
        self._cache[key] = (self._clock() + self._ttl, stored)
        return default if stored is _MISSING else stored

    async def set(self, key: str, value: Any, updated_by: uuid.UUID | None = None) -> None:
        stmt = insert(Setting).values(key=key, value=value, updated_by=updated_by)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Setting.key],
            # updated_at 的 server_default 只在 INSERT 生效，表上也没有触发器，
            # 冲突分支必须自己刷新（clock_timestamp 而非 now()：取真实墙钟）。
            set_={
                "value": value,
                "updated_by": updated_by,
                "updated_at": func.clock_timestamp(),
            },
        )
        async with self._factory() as session:
            await session.execute(stmt)
            await session.commit()
        self._cache[key] = (self._clock() + self._ttl, value)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)
