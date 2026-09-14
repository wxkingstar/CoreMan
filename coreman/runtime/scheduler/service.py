"""scheduler 进程（spec §4.1）：单主看护。

集群里可以起多个 scheduler，同一时刻只有一个在干活——选主靠 PostgreSQL 的会话级咨询锁，锁挂
在一条独立的 asyncpg 连接上：进程一死连接就断、锁自动释放，备用实例几秒内接手，不需要额外的
心跳表、租约表或任何「上一任真的死了吗」的判断。非主实例除了每 `leader_retry_seconds` 试一次
锁之外，不碰任何业务表。

主实例两个节奏：

| 节奏 | 动作 |
| --- | --- |
| `tick_seconds` | 掉线任务收尸、放掉死网关的租约、标记死实例 |
| `cleanup_seconds` | 删过期的流 / 已发出站 / 过期会话，清早已停掉的实例行 |

M4 的 cron 调度每 10 秒独立运行，通知消费者单独投递。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import asyncpg

from coreman import __version__
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.session import make_engine, make_session_factory
from coreman.core.settings_store import SettingsStore
from coreman.runtime.base import HEALTH_PORTS, Service
from coreman.runtime.bus import instances
from coreman.runtime.bus.notify import asyncpg_dsn
from coreman.runtime.scheduler import cron, notifications, reaper

# hashtext 把锁名压成 int4，pg_try_advisory_lock 隐式提升成 bigint；同名即同锁，跨实例通用。
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('coreman-scheduler'))"
LOCK_NAME = "coreman-scheduler"
MAX_HEARTBEAT_SECONDS = 10.0


class SchedulerService(Service):
    """看护进程。

    Attributes:
        ready: `on_start` 跑完了（健康检查与用例看它）
        is_leader: 当前持有咨询锁，正在跑看护循环
    """

    def __init__(
        self,
        *,
        port: int = HEALTH_PORTS["scheduler"],
        tick_seconds: float = 15.0,
        cleanup_seconds: float = 60.0,
        leader_retry_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            "scheduler",
            port=port,
            heartbeat_seconds=min(MAX_HEARTBEAT_SECONDS, tick_seconds),
            log_heartbeat_seconds=30.0,
        )
        self.ready = False
        self.is_leader = False
        self._tick_seconds = tick_seconds
        self._cleanup_seconds = cleanup_seconds
        self._leader_retry = leader_retry_seconds
        # 依赖都是纯构造（不连库、不要事件循环），放 __init__ 里 on_shutdown 才不必处处判空。
        settings = get_settings()
        self._engine = make_engine(settings.database_url)
        self._factory = make_session_factory(self._engine)
        self._store = SettingsStore(self._factory)
        self._dsn = asyncpg_dsn(settings.database_url)
        self._cipher = Cipher(settings.master_key_bytes)
        self._lock_conn: asyncpg.Connection | None = None
        self._bg: list[asyncio.Task[None]] = []

    # ---- 生命周期 ---------------------------------------------------------

    async def on_start(self) -> None:
        try:
            await self._start()
        except Exception:
            # on_start 抛出时基类不会走 on_shutdown，连接池得自己收干净再往上抛。
            await self._engine.dispose()
            raise

    async def _start(self) -> None:
        async with self._factory() as session:
            # capacity 对看护进程没有意义：它不认领任务，只有一个「在不在」。
            await instances.register(
                session,
                instance_id=self.instance_id,
                service=self.name,
                version=__version__,
                capacity=None,
            )
            await session.commit()
        self._bg = [
            asyncio.create_task(coro, name=name)
            for name, coro in (
                ("scheduler-leader", self._leader_loop()),
                ("scheduler-heartbeat", self._heartbeat_loop()),
                ("scheduler-notifications", self._notifications_loop()),
                ("scheduler-cron", self._cron_loop()),
                ("scheduler-escalations", self._escalations_loop()),
                ("scheduler-objects", self._objects_loop()),
                ("scheduler-alerts", self._alerts_loop()),
                ("scheduler-relay-health", self._relay_health_loop()),
            )
        ]
        self.ready = True

    async def on_shutdown(self) -> None:
        self.ready = False
        for job in self._bg:
            job.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        self.is_leader = False
        # 关连接即释放咨询锁：备用实例下一次重试就能接手，不用等任何超时。
        await self._close_lock()
        try:
            async with self._factory() as session:
                await instances.mark_stopped(session, self.instance_id)
                await session.commit()
        except Exception:  # noqa: BLE001 退出路径上写不进去也只能记一笔
            self._log.exception("instance_mark_stopped_failed")
        await self._engine.dispose()

    async def _sleep(self, seconds: float) -> None:
        """睡到点或睡到收停止信号——退出不必等满一个周期。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # ---- 选主 -------------------------------------------------------------

    async def _leader_loop(self) -> None:
        while not self._stop.is_set():
            if not await self._try_lock():
                await self._sleep(self._leader_retry)
                continue
            self.is_leader = True
            self._log.info("scheduler_leader_acquired", lock=LOCK_NAME)
            try:
                await self._tick_loop()
            finally:
                self.is_leader = False
                await self._close_lock()
                self._log.info("scheduler_leader_released")

    async def _try_lock(self) -> bool:
        """在专用连接上试一次咨询锁。连接留着复用，失败才丢掉重连。"""
        try:
            if self._lock_conn is None or self._lock_conn.is_closed():
                self._lock_conn = await asyncpg.connect(self._dsn)
            return bool(await self._lock_conn.fetchval(LOCK_SQL))
        except (OSError, asyncpg.PostgresError) as exc:
            self._log.warning("scheduler_lock_failed", error=type(exc).__name__)
            await self._close_lock()
            return False

    async def _lock_alive(self) -> bool:
        """锁连接还活着吗——断了就等于丢了主，得让出去重新竞争。

        不能只看 `is_closed()`：连接被对端掐掉时，本地往往要到下一次真正发查询才发现。
        """
        conn = self._lock_conn
        if conn is None or conn.is_closed():
            return False
        try:
            await conn.fetchval("SELECT 1")
        except (OSError, asyncpg.PostgresError) as exc:
            self._log.warning("scheduler_lock_lost", error=type(exc).__name__)
            return False
        return True

    async def _close_lock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is None or conn.is_closed():
            return
        try:
            await conn.close(timeout=5)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            self._log.warning("scheduler_lock_close_failed", error=type(exc).__name__)

    # ---- 看护 -------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """持锁期间的主循环。第一轮就顺带跑一次清理：刚接手的实例先把摊子收干净。"""
        next_cleanup = 0.0
        while not self._stop.is_set() and await self._lock_alive():
            now = datetime.now(UTC)
            try:
                counts = await reaper.run_tick(self._factory, now, chat_logs_factory=self._factory)
                if time.monotonic() >= next_cleanup:
                    counts.update(await reaper.run_cleanup(self._factory, self._store, now))
                    next_cleanup = time.monotonic() + self._cleanup_seconds
            except Exception:  # noqa: BLE001 一轮看护出错不能让进程丢主、更不能让它退出
                self._log.exception("scheduler_tick_failed")
            else:
                # 只在真的动过东西时记一行：空转日志每 15 秒一条，几天就把检索淹了。
                hits = {key: value for key, value in counts.items() if value}
                if hits:
                    self._log.info("scheduler_reaped", **hits)
            await self._sleep(self._tick_seconds)

    async def _heartbeat_loop(self) -> None:
        """实例心跳。看护进程没有在跑的任务，running 恒为 0。"""
        while not self._stop.is_set():
            try:
                async with self._factory() as session:
                    await instances.heartbeat(session, self.instance_id, 0)
                    await session.commit()
            except Exception:  # noqa: BLE001 库抖一下不能把心跳循环打死
                self._log.exception("instance_heartbeat_failed")
            await self._sleep(self.heartbeat_seconds)

    async def _notifications_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # 选主循环独占咨询锁连接；这里不能并发在同一 asyncpg 连接执行查询。
                # 瞬时丢主的重叠窗口由 outbox 行锁防止重复领取。
                if self.is_leader:
                    if await notifications.deliver_one(self._factory, self._cipher):
                        continue
            except Exception:
                self._log.exception("notification_delivery_failed")
            await self._sleep(1)

    async def _cron_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.is_leader:
                    now = datetime.now(UTC)
                    await cron.recover_runs(self._factory, now, self._cipher)
                    await cron.run_tick(self._factory, now)
            except Exception:
                self._log.exception("cron_tick_failed")
            await self._sleep(10)

    async def _escalations_loop(self) -> None:
        from coreman.core.escalations.media_recovery import recover
        from coreman.core.escalations.service import tick

        while not self._stop.is_set():
            try:
                if self.is_leader:
                    async with self._factory() as session:
                        await tick(session, datetime.now(UTC))
                        await recover(session, datetime.now(UTC))
                        await session.commit()
            except Exception:
                self._log.exception("escalation_tick_failed")
            await self._sleep(30)

    async def _relay_health_loop(self) -> None:
        from coreman.core.observability.relay_health import tick

        while not self._stop.is_set():
            try:
                if self.is_leader:
                    async with asyncio.timeout(60):
                        await tick(self._factory, datetime.now(UTC))
            except Exception:
                self._log.error("relay_health_evaluation_failed")
            await self._sleep(60)

    async def _alerts_loop(self) -> None:
        from coreman.core.observability.alerts import tick

        while not self._stop.is_set():
            try:
                if self.is_leader:
                    async with asyncio.timeout(10), self._factory() as session:
                        await tick(session, datetime.now(UTC))
                        await session.commit()
            except Exception:
                self._log.error("alerts_evaluation_failed")
            await self._sleep(30)

    async def _objects_loop(self) -> None:
        from coreman.core.object_store import object_store

        cfg = get_settings()
        stores = [object_store(cfg, "local")]
        if cfg.s3_bucket and cfg.s3_access_key and cfg.s3_secret_key:
            stores.append(object_store(cfg, "s3"))
        while not self._stop.is_set():
            try:
                if self.is_leader:
                    for store in stores:
                        async with self._factory() as session:
                            count = await store.cleanup(session, datetime.now(UTC))
                            await session.commit()
                            if count:
                                self._log.info("objects_cleaned", count=count)
            except Exception:
                self._log.exception("objects_cleanup_failed")
            await self._sleep(60)
