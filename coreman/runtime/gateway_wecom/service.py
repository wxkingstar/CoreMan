"""gateway-wecom 进程（spec §4.1、§6.5、§7.3）：租约、连接、入站、推送、排空。

进程内五条后台协程，各管一件事：

| 协程 | 节奏 | 职责 |
| --- | --- | --- |
| `_lease_loop` | `lease_interval` 或 `lease_changed` | 认领、心跳、排空、丢了租约就断连 |
| `_instance_loop` | `heartbeat_seconds` | 实例心跳；被要求排空就顺序排空并退出 |
| `_stream_loop` | `stream_updated` 或 `poll_interval` | 推 `task_streams` 的增量 |
| `_outbox_loop` | `outbox_added` 或 `poll_interval` | 发 `outbox` 的主动消息 |
| `_config_loop` | `config_changed` | bots 行变化：热更新 / 重连 / 停用 |

每个 bot 一个 `BotRunner`（一条 WS）。同一个 bot 只能被一个实例持有，靠 `bot_leases` 抢占；
排空严格按 spec §6.5 的 ①–⑤，让新实例接手时用户最多感觉到 2–5 秒的停顿。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman import __version__
from coreman.core.bus import instances, leases
from coreman.core.bus.notify import Listener, asyncpg_dsn
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.session import make_engine, make_session_factory
from coreman.runtime.base import HEALTH_PORTS, Service
from coreman.runtime.gateway_wecom.runner import BotInfo, BotRunner, load_bot_info
from coreman.runtime.gateway_wecom.ws_client import DEFAULT_WS_CONFIG, WsConfig

PLATFORM = "wecom"
CHANNELS = ("stream_updated", "outbox_added", "lease_changed", "config_changed")
# 排空并发 1、间隔 3 秒（spec §6.5）：同时断一片连接会把下游的重连全挤在一个瞬间。
DRAIN_GAP_SECONDS = 3.0
# 踢线熔断后多久才允许重新认领这个 bot（spec §6.5「停止重连该 bot 30 秒」）。
KICK_COOLDOWN_SECONDS = 30.0


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class GatewayWecomService(Service):
    """企微网关进程。

    Attributes:
        runners: 本实例当前持有连接的 bot
        ready: 后台协程都起来了（测试与健康检查看它）
        draining: 已被要求排空，不再认领新的 bot
    """

    def __init__(
        self,
        *,
        port: int = HEALTH_PORTS["gateway-wecom"],
        lease_interval: float = 5.0,
        heartbeat_seconds: float = 10.0,
        poll_interval: float = 1.0,
        ws_config: WsConfig = DEFAULT_WS_CONFIG,
        stop_grace_seconds: float = 120.0,
    ) -> None:
        super().__init__(
            "gateway-wecom",
            port=port,
            heartbeat_seconds=heartbeat_seconds,
            log_heartbeat_seconds=30.0,
        )
        self.ws_config = ws_config
        self.ready = False
        self.draining = False
        self.runners: dict[uuid.UUID, BotRunner] = {}
        # 正在排空中的 bot：它们暂时「持有租约却没有 runner」，租约扫描必须放着不管。
        self._draining: set[uuid.UUID] = set()
        self._lease_interval = lease_interval
        self._poll = poll_interval
        self._stop_grace = stop_grace_seconds
        # 依赖都是纯构造（不连库、不要事件循环），放 __init__ 里 on_shutdown 才不必处处判空。
        settings = get_settings()
        self._engine = make_engine(settings.database_url)
        self.factory: async_sessionmaker[AsyncSession] = make_session_factory(self._engine)
        self._cipher = Cipher(settings.master_key_bytes)
        self._listener = Listener(asyncpg_dsn(settings.database_url), CHANNELS)
        self._fused: dict[uuid.UUID, float] = {}
        self._bg: list[asyncio.Task[None]] = []

    # ---- 生命周期 ---------------------------------------------------------

    async def on_start(self) -> None:
        try:
            await self._start()
        except Exception:
            # on_start 抛出时基类不会走 on_shutdown，监听连接和连接池得自己收干净再往上抛。
            await self._listener.stop()
            await self._engine.dispose()
            raise

    async def _start(self) -> None:
        await self._listener.start()
        async with self.factory() as session:
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
                ("gateway-lease", self._lease_loop()),
                ("gateway-instance", self._instance_loop()),
                ("gateway-stream", self._stream_loop()),
                ("gateway-outbox", self._outbox_loop()),
                ("gateway-config", self._config_loop()),
            )
        ]
        self.ready = True

    async def on_shutdown(self) -> None:
        self.draining = True
        self.ready = False
        # 先停循环再排空：否则租约扫描会在排空的间隙里把刚放手的 bot 又认领回来。
        for job in self._bg:
            job.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._drain_all()
        try:
            async with self.factory() as session:
                await instances.mark_stopped(session, self.instance_id)
                await session.commit()
        except Exception:  # noqa: BLE001 退出路径上写不进去也只能记一笔
            self._log.exception("instance_mark_stopped_failed")
        await self._listener.stop()
        await self._engine.dispose()

    # ---- 排空 -------------------------------------------------------------

    async def _drain_all(self) -> None:
        deadline = time.monotonic() + self._stop_grace
        for index, bot_id in enumerate(list(self.runners)):
            if index and time.monotonic() + DRAIN_GAP_SECONDS < deadline:
                await asyncio.sleep(DRAIN_GAP_SECONDS)
            if time.monotonic() > deadline:
                self._log.warning("drain_deadline_exceeded", left=len(self.runners))
                return
            await self.drain_bot(bot_id)

    async def drain_bot(self, bot_id: uuid.UUID) -> None:
        """单 bot 排空（spec §6.5）：① 停出站 ② 流补 finish ③ 关 WS ④ 释放租约 ⑤ 通知。

        先把 runner 摘出 `self.runners`，推送 / 出站两条循环就不会再碰它；`runner.drain()`
        里的锁负责等已经在途的那一次发送收尾。

        整段挂在 `self._draining` 里：从摘掉 runner 到释放租约之间，这个 bot 看上去就是
        「持有租约却没有连接」，并发的租约扫描会把它当成认领后启动失败而提前 release——
        §6.5 的 ④ 就跑到了 ③ 前面，排空还没做完租约就交了出去。同一个 bot 并发进来两次的
        话，第二次直接返回：它的 `finally` 会把第一次挂上的标记抹掉。
        """
        if bot_id in self._draining:
            return
        self._draining.add(bot_id)
        try:
            runner = self.runners.pop(bot_id, None)
            if runner is None:
                # 没有 runner 可排空（并发已经收走了）：租约交给 `_lease_round` 的
                # 「持有却没连接」分支去还，那里拿得到行上的代次。
                return
            try:
                await runner.drain()  # ①②
            except Exception:  # noqa: BLE001 排空不能因为一条流推不动就卡住
                self._log.exception("drain_streams_failed", bot_key=runner.bot.bot_key)
            await runner.stop()  # ③
            async with self.factory() as session:
                # ④ 释放租约；⑤ lease_changed 通知在 release 里同事务发出
                await leases.release(
                    session,
                    bot_id=bot_id,
                    instance_id=self.instance_id,
                    generation=runner.generation,
                )
                await session.commit()
            self._log.info("bot_drained", bot_id=str(bot_id), generation=runner.generation)
        finally:
            self._draining.discard(bot_id)

    def note_fused(self, bot_id: uuid.UUID) -> None:
        """WS 踢线熔断：记下冷却时刻，租约扫描那一轮把它交还出去。"""
        self._fused[bot_id] = time.monotonic() + KICK_COOLDOWN_SECONDS

    async def _release_fused(self, bot_id: uuid.UUID) -> None:
        runner = self.runners.pop(bot_id, None)
        if runner is None:
            return
        await runner.stop()
        async with self.factory() as session:
            # 释放时就把状态落成 kicked（不能释放完再补写：那一行已经不归本实例，
            # set_state 的持有者守卫会挡掉），运维一眼看出是被抢连踢掉的。
            await leases.release(
                session,
                bot_id=bot_id,
                instance_id=self.instance_id,
                generation=runner.generation,
                state="kicked",
            )
            await session.commit()
        self._log.warning("lease_released_after_kick_fuse", bot_id=str(bot_id))

    async def _stop_orphan(self, bot_id: uuid.UUID) -> None:
        """租约已经不在本实例名下（被别人抢走、或被巡检释放），连接却还开着：立刻断掉。

        **不调 `leases.release`**：那一行现在是别人的，释放它等于把新持有者踢下台。只收自己
        这一半——WS 一断，企微那边就只剩新持有者的连接，不会两个实例互踢到熔断为止；这条流
        与出站箱此后归新持有者管（它们各自的代次校验会挡掉本实例的残余动作）。
        """
        runner = self.runners.pop(bot_id, None)
        if runner is None:
            return
        self._log.warning("lease_lost", bot_key=runner.bot.bot_key, bot_id=str(bot_id))
        await runner.stop()

    # ---- 租约 -------------------------------------------------------------

    async def _lease_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._lease_round()
            except Exception:  # noqa: BLE001 库抖一下不能把认领循环打死
                self._log.exception("lease_loop_error")
            # 顺带取空 lease_changed 的队列（没人消费它会一直堆在 Listener 里）；
            # 别人释放租约时我们还能立刻醒过来接手。
            await self._listener.wait("lease_changed", timeout=self._lease_interval)

    async def _lease_round(self) -> None:
        for bot_id in [b for b in self._fused if b in self.runners]:
            await self._release_fused(bot_id)
        async with self.factory() as session:
            # 先续心跳再干活（M-d）：认领、排空、停孤儿都可能慢，心跳压在后面就会在库抖动
            # 的时候让别的实例觉得本实例死了，把还在正常服务的 bot 抢走。
            await leases.heartbeat(session, self.instance_id)
            await leases.ensure_rows(session, PLATFORM)
            candidates = [] if self.draining else await leases.acquirable(session, PLATFORM)
            await session.commit()
        for bot_id in self._cooled_down(candidates):
            await self._try_acquire(bot_id)
        async with self.factory() as session:
            held = await leases.held_by(session, self.instance_id)
            # 兜底复核 `enabled`：`config_changed` 会在 LISTEN 重连的窗口里丢，只靠通知的话
            # 停用的机器人会一直挂着连接到进程重启（spec §6.1 要求每个监听者都有兜底轮询）。
            disabled = await leases.disabled_among(session, [x.bot_id for x in held])
            await session.commit()
        held_ids = {lease.bot_id for lease in held}
        for bot_id in [b for b in self.runners if b not in held_ids]:
            await self._stop_orphan(bot_id)
        for lease in held:
            if lease.bot_id in self._draining:
                continue  # 正在排空：它的租约由 drain_bot 自己按代次交还
            if lease.bot_id in disabled and lease.bot_id in self.runners:
                self._log.info("bot_disabled_by_poll", bot_id=str(lease.bot_id))
                await self.drain_bot(lease.bot_id)
            elif lease.drain_requested_by and lease.bot_id in self.runners:
                self._log.info("bot_drain_requested", by=lease.drain_requested_by)
                await self.drain_bot(lease.bot_id)
            elif lease.bot_id not in self.runners:
                # 持有租约却没有连接（认领后启动失败、熔断释放的竞态）：交回去，别占着。
                async with self.factory() as session:
                    await leases.release(
                        session,
                        bot_id=lease.bot_id,
                        instance_id=self.instance_id,
                        generation=int(lease.generation),
                    )
                    await session.commit()

    def _cooled_down(self, candidates: list[uuid.UUID]) -> list[uuid.UUID]:
        now = time.monotonic()
        self._fused = {b: until for b, until in self._fused.items() if until > now}
        return [b for b in candidates if b not in self._fused]

    async def _try_acquire(self, bot_id: uuid.UUID) -> None:
        async with self.factory() as session:
            lease = await leases.acquire(
                session, bot_id=bot_id, platform=PLATFORM, instance_id=self.instance_id
            )
            info = await load_bot_info(session, self._cipher, bot_id) if lease else None
            await session.commit()
        if lease is None:
            return
        if info is None:
            async with self.factory() as session:
                await leases.release(
                    session,
                    bot_id=bot_id,
                    instance_id=self.instance_id,
                    generation=int(lease.generation),
                )
                await session.commit()
            return
        await self._start_runner(info, int(lease.generation))

    async def _start_runner(self, info: BotInfo, generation: int) -> None:
        runner = BotRunner(self, info, generation)
        self.runners[info.id] = runner
        await runner.start()
        self._log.info(
            "bot_runner_started",
            bot_key=info.bot_key,
            generation=generation,
            fingerprint=info.credentials_fingerprint,
        )

    # ---- 实例心跳 ---------------------------------------------------------

    async def _instance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with self.factory() as session:
                    drain = await instances.heartbeat(
                        session, self.instance_id, running=len(self.runners)
                    )
                    await session.commit()
                if drain and not self.draining:
                    self.draining = True
                    self._log.info("instance_drain_requested", bots=len(self.runners))
                    await self._drain_all()
                    self.request_stop("drained")
                    return
            except Exception:  # noqa: BLE001 心跳失败下一轮再来
                self._log.exception("instance_heartbeat_failed")
            await asyncio.sleep(self.heartbeat_seconds)

    # ---- 推送 / 出站 / 配置 ------------------------------------------------

    def _targets(self, payloads: list[dict[str, Any]]) -> list[BotRunner]:
        """通知里点名的 bot；没点名（兜底轮询）就扫全部持有的 bot。"""
        ids = {u for p in payloads if (u := _as_uuid(p.get("bot_id"))) is not None}
        return [r for bot_id in (ids or set(self.runners)) if (r := self.runners.get(bot_id))]

    async def _stream_loop(self) -> None:
        while not self._stop.is_set():
            payloads = await self._listener.wait("stream_updated", timeout=self._poll)
            for runner in self._targets(payloads):
                try:
                    await runner.pusher.push_pending()
                except Exception:  # noqa: BLE001 一个 bot 推挂了不能带走其它 bot
                    self._log.exception("stream_push_failed", bot_key=runner.bot.bot_key)

    async def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            payloads = await self._listener.wait("outbox_added", timeout=self._poll)
            for runner in self._targets(payloads):
                try:
                    await runner.outbox.consume()
                except Exception:  # noqa: BLE001 同上
                    self._log.exception("outbox_consume_failed", bot_key=runner.bot.bot_key)

    async def _config_loop(self) -> None:
        while not self._stop.is_set():
            for payload in await self._listener.wait("config_changed", timeout=self._poll):
                if payload.get("table") != "bots":
                    continue  # relay_servers / settings 与网关无关
                bot_id = _as_uuid(payload.get("id"))
                if bot_id is None:
                    continue
                try:
                    await self._apply_bot_change(bot_id)
                except Exception:  # noqa: BLE001 配置应用失败不能把循环打死
                    self._log.exception("bot_config_apply_failed", bot_id=str(bot_id))

    async def _apply_bot_change(self, bot_id: uuid.UUID) -> None:
        """bots 行变了：停用/删除→交还租约；凭证变→重连；其余字段原地更新，不断连。"""
        runner = self.runners.get(bot_id)
        if runner is None:
            return
        async with self.factory() as session:
            info = await load_bot_info(session, self._cipher, bot_id)
        if info is None:
            self._log.info("bot_disabled_or_gone", bot_key=runner.bot.bot_key)
            await self.drain_bot(bot_id)
            return
        if info.credentials_fingerprint != runner.bot.credentials_fingerprint:
            self._log.info(
                "bot_credentials_changed",
                bot_key=info.bot_key,
                fingerprint=info.credentials_fingerprint,
            )
            await runner.reconnect(info)
            return
        runner.bot = info
