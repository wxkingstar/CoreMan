"""飞书网关 supervisor：父进程持租约，每机器人一个 SDK 子进程。

租约规则（认领、排空、孤儿回收）在 `gateway_common.lease_loop.LeaseCoordinator`；这里只管
子进程：认领到租约就拉起，凭证或代次变了就重启，崩溃了按 5→60 秒退避重启，释放租约前先停掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman import __version__
from coreman.core.bus import instances, leases
from coreman.core.config import get_settings
from coreman.core.db.models import Bot, BotLease
from coreman.core.db.session import make_engine, make_session_factory
from coreman.runtime.base import HEALTH_PORTS, Service
from coreman.runtime.gateway_common.lease_loop import LeaseCoordinator


@dataclass
class Child:
    generation: int
    fingerprint: str
    process: asyncio.subprocess.Process | None = None
    next_start: float = 0
    delay: float = 5
    started_at: float = 0


def _fingerprint(bot: Bot) -> str:
    return hashlib.sha256(bot.credentials_enc.encode()).hexdigest()


class GatewayFeishuService(Service):
    def __init__(self, *, port: int = HEALTH_PORTS["gateway-feishu"], interval: float = 5):
        super().__init__("gateway-feishu", port, heartbeat_seconds=10)
        self._engine = make_engine(get_settings().database_url)
        self.factory: async_sessionmaker[AsyncSession] = make_session_factory(self._engine)
        self.children: dict[uuid.UUID, Child] = {}
        self.interval = interval
        # 租约心跳和实例心跳一起在 `_heartbeat_loop` 里写，扫描本身不再续一遍。
        self.lease_loop = LeaseCoordinator(
            platform="feishu",
            instance_id=lambda: self.instance_id,
            factory=self.factory,
            hooks=self,
            heartbeat=False,
        )
        self.ready = False
        self._jobs: list[asyncio.Task[None]] = []

    @property
    def draining(self) -> bool:
        return self.lease_loop.draining

    @draining.setter
    def draining(self, value: bool) -> None:
        self.lease_loop.draining = value

    async def on_start(self) -> None:
        try:
            await self._start()
        except Exception:
            await self._engine.dispose()
            raise

    async def _start(self) -> None:
        async with self.factory() as session:
            await instances.register(
                session,
                instance_id=self.instance_id,
                service=self.name,
                version=__version__,
                capacity=None,
            )
            await session.commit()
        self._jobs = [
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        self.ready = True

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                async with self.factory() as session:
                    await leases.heartbeat(session, self.instance_id)
                    drain = await instances.heartbeat(
                        session, self.instance_id, running=len(self.children)
                    )
                    await session.commit()
                if drain and not self.draining:
                    self.draining = True
                    self.request_stop("drained")
            except Exception:
                self._log.error("feishu_heartbeat_failed")
            await asyncio.sleep(self.interval)

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                self._log.error("feishu_reconcile_failed")
            await asyncio.sleep(self.interval)

    async def spawn(self, bot_id: uuid.UUID, child: Child) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "coreman.runtime.gateway_feishu.child",
            "--bot-id",
            str(bot_id),
            "--instance-id",
            self.instance_id,
            "--generation",
            str(child.generation),
            "--parent-pid",
            str(os.getpid()),
        )

    async def reconcile(self) -> None:
        await self.lease_loop.round()

    # ---- 租约钩子 ---------------------------------------------------------

    def lease_connected(self) -> Collection[uuid.UUID]:
        return self.children.keys()

    async def lease_connect(self, bot_id: uuid.UUID, generation: int) -> bool:
        async with self.factory() as session:
            bot = await session.get(Bot, bot_id)
        if bot is None or not bot.enabled:
            return False
        if bot_id in self.children:
            # 上一代的子进程还在（释放与再认领落在同一轮）：先停掉，新旧两代不能同时连着。
            await self.stop_child(bot_id, release=False)
        child = Child(generation, _fingerprint(bot))
        self.children[bot_id] = child
        await self._supervise(bot_id, child)
        return True

    async def lease_orphaned(self, bot_id: uuid.UUID) -> None:
        await self.stop_child(bot_id, release=False)

    async def lease_drain(self, bot_id: uuid.UUID) -> None:
        await self.stop_child(bot_id)

    async def lease_maintain(self, held: list[BotLease]) -> None:
        async with self.factory() as session:
            snapshots = {
                bot.id: bot
                for bot in await session.scalars(
                    select(Bot).where(Bot.id.in_([lease.bot_id for lease in held]))
                )
            }
        for lease in held:
            bot = snapshots.get(lease.bot_id)
            child = self.children.get(lease.bot_id)
            if bot is None or child is None:
                continue
            fingerprint = _fingerprint(bot)
            if child.fingerprint != fingerprint or child.generation != lease.generation:
                await self.stop_child(bot.id, release=False)
                child = Child(int(lease.generation), fingerprint)
                self.children[bot.id] = child
            await self._supervise(bot.id, child)

    async def _supervise(self, bot_id: uuid.UUID, child: Child) -> None:
        """子进程退出了就按退避重启（跑满 120 秒才算稳定，退避归零）；到点了就拉起。"""
        now = time.monotonic()
        if child.process and child.process.returncode is not None:
            if now - child.started_at > 120:
                child.delay = 5
            child.process = None
            child.next_start = now + child.delay
            child.delay = min(60, child.delay * 2)
            self._log.warning("feishu_child_exited", bot_id=str(bot_id), retry_seconds=child.delay)
            async with self.factory() as session:
                await leases.set_state(
                    session, bot_id, "disconnected", instance_id=self.instance_id
                )
                await session.commit()
        if child.process is None and now >= child.next_start:
            try:
                child.process = await self.spawn(bot_id, child)
                child.started_at = now
            except OSError:
                child.next_start = now + child.delay
                child.delay = min(60, child.delay * 2)
                self._log.error("feishu_child_spawn_failed", bot_id=str(bot_id))

    async def stop_child(self, bot_id: uuid.UUID, *, release: bool = True) -> None:
        child = self.children.get(bot_id)
        if child is None:
            return
        if child.process and child.process.returncode is None:
            try:
                child.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(child.process.wait(), timeout=10)
            except TimeoutError:
                child.process.kill()
                await child.process.wait()
        self.children.pop(bot_id, None)
        if release:
            await self.lease_loop.release(bot_id, child.generation)

    async def on_shutdown(self) -> None:
        self.ready, self.draining = False, True
        self._jobs[0].cancel()
        await asyncio.gather(self._jobs[0], return_exceptions=True)
        try:
            async with asyncio.timeout(100):
                for index, bot_id in enumerate(list(self.children)):
                    if index:
                        await asyncio.sleep(3)
                    await self.stop_child(bot_id)
        except TimeoutError:
            for child in self.children.values():
                if child.process and child.process.returncode is None:
                    child.process.kill()
            await asyncio.gather(
                *(c.process.wait() for c in self.children.values() if c.process),
                return_exceptions=True,
            )
            self.children.clear()
        finally:
            self._jobs[1].cancel()
            await asyncio.gather(self._jobs[1], return_exceptions=True)
            async with self.factory() as session:
                await instances.mark_stopped(session, self.instance_id)
                await session.commit()
            await self._engine.dispose()
