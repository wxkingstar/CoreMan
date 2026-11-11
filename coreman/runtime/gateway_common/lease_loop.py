"""网关租约循环：认领、续期、排空、孤儿回收，企微与飞书共用同一份规则。

平台只实现连接相关的钩子（`LeaseHooks`）：

| 钩子 | 何时调用 | 企微 | 飞书 |
| --- | --- | --- | --- |
| `lease_connected()` | 每轮 | 持有 runner 的 bot | 有子进程记录的 bot |
| `lease_connect(bot_id, generation)` | 刚认领到租约 | 解密凭证、起 WS | 起 SDK 子进程 |
| `lease_orphaned(bot_id)` | 租约已不在本实例名下 | 断 WS（不释放） | 停子进程（不释放） |
| `lease_drain(bot_id)` | 停用 / 被要求排空 | 排空流、断 WS、按代次释放 | 停子进程、按代次释放 |
| `lease_maintain(leases)` | 每轮，对仍在服务的租约 | 无 | 凭证变更重启、崩溃退避重启 |

规则只在这里写一份：先续心跳再干活；实例排空时不认领；冷却中的 bot 不认领；持有租约却
没有连接（启动失败、竞态）就交回去；排空中的 bot 由 `lease_drain` 自己按代次交还，扫描不碰。

租约放在哪张表由 `store` 决定：默认是按 bot 的 `bot_leases`（`BotLeases`），飞书网关另有一个
扫描器用 `core.bus.app_leases`（个人助理应用，一个应用一条连接）。钩子里的 `bot_id` 在后一种
情况下是 platform_apps.id。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection, Sequence
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import leases
from coreman.core.logging import get_logger


class LeaseHooks(Protocol):
    def lease_connected(self) -> Collection[uuid.UUID]:
        """本实例此刻有连接（或子进程记录）的 bot。"""
        ...

    async def lease_connect(self, bot_id: uuid.UUID, generation: int) -> bool:
        """刚认领到这一代租约：建连接。返回 False（bot 不可用）时租约随即交回。"""
        ...

    async def lease_orphaned(self, bot_id: uuid.UUID) -> None:
        """租约已经归别人了：只断自己这一半，**不释放**（那一行不是本实例的了）。"""
        ...

    async def lease_drain(self, bot_id: uuid.UUID) -> None:
        """排空并按自己持有的代次释放租约。"""
        ...

    async def lease_maintain(self, held: list[Any]) -> None:
        """对仍在正常服务的租约做平台自己的维护（每轮一次，可以什么都不做）。"""
        ...


class LeaseStore(Protocol):
    """租约表的读写。`key` 是这张表的主键：bot_leases 是 bot id，app_leases 是应用 id。

    `held_by` 返回的行都带 `key`、`generation`、`drain_requested_by`。
    """

    async def heartbeat(self, session: AsyncSession, instance_id: str) -> int: ...

    async def ensure_rows(self, session: AsyncSession) -> None: ...

    async def acquirable(self, session: AsyncSession) -> list[uuid.UUID]: ...

    async def acquire(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str
    ) -> int | None: ...

    async def release(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str, generation: int, state: str
    ) -> None: ...

    async def held_by(self, session: AsyncSession, instance_id: str) -> list[Any]: ...

    async def disabled_among(
        self, session: AsyncSession, keys: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]: ...


class BotLeases:
    """按 bot 的租约（`bot_leases`）：把 `core.bus.leases` 包成 `LeaseStore`。"""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    async def heartbeat(self, session: AsyncSession, instance_id: str) -> int:
        return await leases.heartbeat(session, instance_id)

    async def ensure_rows(self, session: AsyncSession) -> None:
        await leases.ensure_rows(session, self.platform)

    async def acquirable(self, session: AsyncSession) -> list[uuid.UUID]:
        return await leases.acquirable(session, self.platform)

    async def acquire(self, session: AsyncSession, key: uuid.UUID, instance_id: str) -> int | None:
        lease = await leases.acquire(
            session, bot_id=key, platform=self.platform, instance_id=instance_id
        )
        return None if lease is None else int(lease.generation)

    async def release(
        self, session: AsyncSession, key: uuid.UUID, instance_id: str, generation: int, state: str
    ) -> None:
        await leases.release(
            session, bot_id=key, instance_id=instance_id, generation=generation, state=state
        )

    async def held_by(self, session: AsyncSession, instance_id: str) -> list[Any]:
        return list(await leases.held_by(session, instance_id))

    async def disabled_among(
        self, session: AsyncSession, keys: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return await leases.disabled_among(session, keys)


class LeaseCoordinator:
    """一个网关实例的租约扫描器。

    Attributes:
        draining: 实例已被要求排空：不再认领新的 bot
        busy: 正在排空中的 bot——它们暂时「持有租约却没有连接」，扫描必须放着不管
    """

    def __init__(
        self,
        *,
        platform: str,
        instance_id: Callable[[], str],
        factory: async_sessionmaker[AsyncSession],
        hooks: LeaseHooks,
        heartbeat: bool = True,
        store: LeaseStore | None = None,
    ) -> None:
        self.platform = platform
        # 每次现取：进程的实例 id 允许在构造之后才定下来（运维脚本、演练用例会改写它）。
        self._instance_id = instance_id
        self.factory = factory
        self.hooks = hooks
        self.heartbeat = heartbeat
        self.store: LeaseStore = store or BotLeases(platform)
        self.draining = False
        self.busy: set[uuid.UUID] = set()
        self._cooldown: dict[uuid.UUID, float] = {}
        self._log = get_logger(__name__).bind(platform=platform)

    @property
    def instance_id(self) -> str:
        return self._instance_id()

    def cool_down(self, bot_id: uuid.UUID, seconds: float) -> None:
        """`seconds` 秒内不认领这个 bot（例如踢线熔断后别和抢连的那一方打起来）。"""
        self._cooldown[bot_id] = time.monotonic() + seconds

    async def round(self) -> None:
        async with self.factory() as session:
            # 先续心跳再干活：认领、排空、停孤儿都可能慢，心跳压在后面就会在库抖动的时候
            # 让别的实例觉得本实例死了，把还在正常服务的 bot 抢走。
            if self.heartbeat:
                await self.store.heartbeat(session, self.instance_id)
            await self.store.ensure_rows(session)
            candidates = [] if self.draining else await self.store.acquirable(session)
            await session.commit()
        for bot_id in self._cooled_down(candidates):
            await self._try_acquire(bot_id)
        async with self.factory() as session:
            held = await self.store.held_by(session, self.instance_id)
            # 兜底复核 `enabled`：`config_changed` 会在 LISTEN 重连的窗口里丢，只靠通知的话
            # 停用的机器人会一直挂着连接到进程重启（每个监听者都必须有兜底轮询）。
            disabled = await self.store.disabled_among(session, [x.key for x in held])
            await session.commit()
        held_ids = {lease.key for lease in held}
        for bot_id in [b for b in self.hooks.lease_connected() if b not in held_ids]:
            await self.hooks.lease_orphaned(bot_id)
        serving: list[Any] = []
        for lease in held:
            if lease.key in self.busy:
                continue  # 正在排空：它的租约由 lease_drain 自己按代次交还
            connected = lease.key in self.hooks.lease_connected()
            if connected and lease.key in disabled:
                self._log.info("bot_disabled_by_poll", bot_id=str(lease.key))
                await self.drain(lease.key)
            elif connected and lease.drain_requested_by:
                self._log.info("bot_drain_requested", by=lease.drain_requested_by)
                await self.drain(lease.key)
            elif not connected:
                # 持有租约却没有连接（认领后启动失败、熔断释放的竞态）：交回去，别占着。
                await self.release(lease.key, int(lease.generation))
            else:
                serving.append(lease)
        if serving:
            await self.hooks.lease_maintain(serving)

    async def drain(self, bot_id: uuid.UUID) -> None:
        """单 bot 排空；整段挂在 `busy` 里。

        从摘掉连接到释放租约之间，这个 bot 看上去就是「持有租约却没有连接」，并发的扫描会把
        它当成认领后启动失败而提前 release——释放租约就跑到了关连接前面。同一个 bot 并发进来
        两次的话，第二次直接返回：它的 `finally` 会把第一次挂上的标记抹掉。
        """
        if bot_id in self.busy:
            return
        self.busy.add(bot_id)
        try:
            await self.hooks.lease_drain(bot_id)
        finally:
            self.busy.discard(bot_id)

    async def release(
        self, bot_id: uuid.UUID, generation: int, *, state: str = "disconnected"
    ) -> None:
        async with self.factory() as session:
            await self.store.release(session, bot_id, self.instance_id, generation, state)
            await session.commit()

    def _cooled_down(self, candidates: list[uuid.UUID]) -> list[uuid.UUID]:
        now = time.monotonic()
        self._cooldown = {b: until for b, until in self._cooldown.items() if until > now}
        return [b for b in candidates if b not in self._cooldown]

    async def _try_acquire(self, bot_id: uuid.UUID) -> None:
        async with self.factory() as session:
            generation = await self.store.acquire(session, bot_id, self.instance_id)
            await session.commit()
        if generation is None:
            return
        try:
            connected = await self.hooks.lease_connect(bot_id, generation)
        except Exception:  # noqa: BLE001 一个 bot 起不来不能拖垮整轮扫描
            self._log.exception("lease_connect_failed", bot_id=str(bot_id))
            connected = False
        if not connected:
            await self.release(bot_id, generation)
