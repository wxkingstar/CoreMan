"""pg_notify / LISTEN 通道（spec §6.1）。通知与写操作同事务；消费者必须有 1 秒兜底轮询。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.logging import get_logger

CHANNELS = (
    "tasks_queued",
    "task_cancel",
    "stream_updated",
    "outbox_added",
    "lease_changed",
    "config_changed",
)
log = get_logger(__name__)


def asyncpg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def notify(session: AsyncSession, channel: str, payload: dict[str, Any]) -> None:
    await session.execute(
        text("SELECT pg_notify(:ch, :payload)"),
        {"ch": channel, "payload": json.dumps(payload, ensure_ascii=False, default=str)},
    )


class Listener:
    """独立 asyncpg 连接上 LISTEN 若干通道；断线自动重连、定期探活。"""

    def __init__(
        self,
        dsn: str,
        channels: Sequence[str],
        *,
        reconnect_base: float = 1.0,
        probe_interval: float = 15.0,
    ) -> None:
        self._dsn, self._channels, self._base = dsn, list(channels), reconnect_base
        self._probe_interval = probe_interval
        self._next_probe = 0.0
        self._conn: asyncpg.Connection | None = None
        self._events: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._payloads: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    async def start(self) -> None:
        await self._connect()
        self._task = asyncio.create_task(self._supervise(), name="pg-listener")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._conn and not self._conn.is_closed():
            await self._conn.close()

    async def wait(self, channel: str, timeout: float = 1.0) -> list[dict[str, Any]]:  # noqa: ASYNC109
        """取走该通道自上次 wait 以来累积的载荷；超时返回空列表，其它通道不受影响。

        `timeout` 是这个接口的语义本身（消费者「等通知或到点兜底轮询」），不是可以外推给
        `asyncio.timeout` 的调用超时，故豁免 ASYNC109。
        """
        ev = self._events[channel]
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass
        ev.clear()
        q = self._payloads[channel]
        out = list(q)
        q.clear()
        return out

    def _on_notify(self, _conn: object, _pid: int, channel: str, payload: str) -> None:
        try:
            data: dict[str, Any] = json.loads(payload) if payload else {}
        except ValueError:
            data = {"raw": payload}
        self._payloads[channel].append(data)
        self._events[channel].set()

    async def _connect(self) -> None:
        old, self._conn = self._conn, None
        if old is not None and not old.is_closed():
            with contextlib.suppress(OSError, asyncpg.PostgresError, TimeoutError):
                await old.close(timeout=5)
        self._conn = await asyncpg.connect(self._dsn)
        for ch in self._channels:
            await self._conn.add_listener(ch, self._on_notify)
        self._next_probe = time.monotonic() + self._probe_interval

    async def _alive(self) -> bool:
        """连接还活着吗——只看 `is_closed()` 不够。

        LISTEN 连接被对端（网络设备、PgBouncer、数据库重启）悄悄掐掉时，本地往往要到下一次
        真发查询才发现；在那之前它既不报错也收不到任何通知，所有靠通知唤醒的循环都退化成只
        剩兜底轮询。所以定期打一次 `SELECT 1`（同 scheduler 的 `_lock_alive`），探不通就重连。
        """
        conn = self._conn
        if conn is None or conn.is_closed():
            return False
        if time.monotonic() < self._next_probe:
            return True
        try:
            await conn.fetchval("SELECT 1")
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("pg_listener_probe_failed", error=type(exc).__name__)
            return False
        self._next_probe = time.monotonic() + self._probe_interval
        return True

    async def _supervise(self) -> None:
        delay = self._base
        while not self._stopping:
            await asyncio.sleep(delay if not self.connected else self._base)
            if await self._alive():
                delay = self._base
                continue
            try:
                await self._connect()
                log.info("pg_listener_reconnected")
                delay = self._base
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("pg_listener_reconnect_failed", error=type(exc).__name__)
                delay = min(delay * 2, 10.0)
