"""worker 进程（spec §4.1、§6.2、§6.3）：认领任务、心跳、排空、把任务交给处理器。

进程内只有五个后台协程：两条车道各一个认领循环、一个心跳循环、一个取消循环、一个配置失效
循环。任务本身跑在各自的 `asyncio.Task` 里，处理器崩了也只影响那一个任务——`_run` 兜住所有
异常并把失败写回 tasks / task_streams，绝不让服务退出。

唤醒有三条路：`tasks_queued` 通知、1 秒兜底轮询、以及「有任务跑完腾出槽位」的进程内事件。
第三条不能省：只靠前两条时，一个满载的 worker 要等到下一次兜底轮询才会补位。
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from coreman import __version__
from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.notify import Listener, asyncpg_dsn
from coreman.core.chat.chat_logs import ChatLogWriter
from coreman.core.chat.openuserid import OpenUseridResolver
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer, Task
from coreman.core.db.session import make_engine, make_session_factory
from coreman.core.i18n.messages import msg
from coreman.core.relay.client import RelayClient
from coreman.core.settings_schema import SETTING_DEFAULTS
from coreman.core.settings_store import SettingsStore
from coreman.core.wecom.media import MediaFetcher
from coreman.runtime.base import HEALTH_PORTS, Service
from coreman.runtime.worker.context import TaskContext

LANES = ("normal", "fast")
CHANNELS = ("tasks_queued", "task_cancel", "config_changed")


class TaskHandler(Protocol):
    """一种 task kind 的处理器。`run` 返回即视为处理完毕（含自己写 finish）。"""

    kind: str

    async def run(self, ctx: TaskContext) -> None: ...


def default_relay_client(relay: RelayServer) -> RelayClient:
    return RelayClient(relay.relay_url)


class WorkerService(Service):
    def __init__(
        self,
        *,
        port: int = HEALTH_PORTS["worker"],
        handlers: Mapping[str, TaskHandler] | None = None,
        poll_interval: float = 1.0,
        relay_client_factory: Callable[[RelayServer], RelayClient] | None = None,
        max_concurrent_override: int | None = None,
        fast_slots_override: int | None = None,
        heartbeat_seconds: float = 10.0,
        stop_grace_seconds: float = 7200.0,
        openuserid: OpenUseridResolver | None = None,
        media_fetcher: MediaFetcher | None = None,
    ) -> None:
        super().__init__(
            "worker", port=port, heartbeat_seconds=heartbeat_seconds, log_heartbeat_seconds=30.0
        )
        self._handlers: dict[str, TaskHandler] = dict(handlers or {})
        self._poll = poll_interval
        self._relay_factory = relay_client_factory or default_relay_client
        self._max_override = max_concurrent_override
        self._fast_override = fast_slots_override
        self._stop_grace = stop_grace_seconds
        self.ready = False
        self.draining = False
        # 依赖都是纯构造（不连库、不要事件循环），放在 __init__ 里 on_shutdown 才不必处处判空。
        settings = get_settings()
        self._engine = make_engine(settings.database_url)
        self._factory = make_session_factory(self._engine)
        self._store = SettingsStore(self._factory)
        self._cipher = Cipher(settings.master_key_bytes)
        self._chat_logs = ChatLogWriter(self._factory)
        # 这两个可注入（测试用假客户端）；没给就在 _start 里建默认的，全进程共享一份缓存/连接池。
        self._openuserid = openuserid
        self._media_fetcher = media_fetcher
        self._listener = Listener(asyncpg_dsn(settings.database_url), CHANNELS)
        self._running: dict[int, tuple[asyncio.Task[None], TaskContext]] = {}
        self._wake: dict[str, asyncio.Event] = {lane: asyncio.Event() for lane in LANES}
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
        if self._openuserid is None:
            self._openuserid = OpenUseridResolver(self._factory, self._cipher)
        if self._media_fetcher is None:
            self._media_fetcher = MediaFetcher()
        await self._listener.start()
        async with self._factory() as session:
            await instances.register(
                session,
                instance_id=self.instance_id,
                service="worker",
                version=__version__,
                capacity=await self._max_concurrent(),
            )
            await session.commit()
        self._bg = [
            asyncio.create_task(coro, name=name)
            for name, coro in (
                ("worker-claim-normal", self._claim_loop("normal")),
                ("worker-claim-fast", self._claim_loop("fast")),
                ("worker-heartbeat", self._heartbeat_loop()),
                ("worker-cancel", self._cancel_loop()),
                ("worker-config", self._config_loop()),
            )
        ]
        self.ready = True

    async def on_shutdown(self) -> None:
        self.draining = True
        self.ready = False
        deadline = time.monotonic() + self._stop_grace
        # 循环而不是一次 wait：认领循环可能在 _stop 置位的同一瞬间又抓了一个任务进来。
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait([run for run, _ctx in self._running.values()], timeout=remaining)
        stragglers = [run for run, _ctx in self._running.values()]
        if stragglers:
            self._log.warning("tasks_cancelled_on_exit", count=len(stragglers))
            for run in stragglers:
                run.cancel()
            await asyncio.gather(*stragglers, return_exceptions=True)
        for job in self._bg:
            job.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        try:
            async with self._factory() as session:
                await instances.mark_stopped(session, self.instance_id)
                await session.commit()
        except Exception:  # noqa: BLE001 退出路径上写不进去也只能记一笔
            self._log.exception("instance_mark_stopped_failed")
        await self._chat_logs.drain(10)
        await self._listener.stop()
        if self._media_fetcher is not None:
            await self._media_fetcher.aclose()
        await self._engine.dispose()

    # ---- 槽位 -------------------------------------------------------------

    async def _setting(self, key: str, override: int | None) -> int:
        if override is not None:
            return override
        return int(await self._store.get(key, SETTING_DEFAULTS[key]))

    async def _max_concurrent(self) -> int:
        return await self._setting("max_concurrent_tasks", self._max_override)

    async def _fast_slots(self) -> int:
        return await self._setting("fast_lane_slots", self._fast_override)

    async def _slots(self, lane: str) -> int:
        """这条车道还能再接几个任务。fast 独占 fast_lane_slots，normal 拿剩下的。"""
        fast = await self._fast_slots()
        limit = fast if lane == "fast" else max(await self._max_concurrent() - fast, 0)
        used = sum(1 for _run, ctx in self._running.values() if ctx.task.lane == lane)
        return limit - used

    # ---- 认领 -------------------------------------------------------------

    async def _claim_loop(self, lane: str) -> None:
        wake = self._wake[lane]
        while not self._stop.is_set():
            # 先清再判：清完到判之间有任务跑完，这一轮的槽位检查就已经看得见它腾出的位置；
            # 判完到 wait 之间跑完，事件已经被重新置位，wait 立即返回。两头都不会丢唤醒。
            wake.clear()
            claimed: Task | None = None
            try:
                if not self.draining and await self._slots(lane) > 0:
                    async with self._factory() as session:
                        claimed = await tasks.claim(
                            session, lane=lane, instance_id=self.instance_id
                        )
                        await session.commit()
            except Exception:  # noqa: BLE001 库抖一下不能把认领循环打死
                self._log.exception("task_claim_failed", lane=lane)
                await asyncio.sleep(self._poll)
                continue
            if claimed is not None:
                self._spawn(claimed)
                continue
            await self._wait_for_work(wake)

    async def _wait_for_work(self, wake: asyncio.Event) -> None:
        """等「有新任务入队」或「有槽位腾出」，`poll_interval` 到点兜底返回。"""
        waiters: list[asyncio.Task[object]] = [
            asyncio.ensure_future(self._listener.wait("tasks_queued", timeout=self._poll)),
            asyncio.ensure_future(wake.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _spawn(self, task: Task) -> None:
        ctx = TaskContext(
            task=task,
            instance_id=self.instance_id,
            session_factory=self._factory,
            settings_store=self._store,
            cipher=self._cipher,
            relay_client_factory=self._relay_factory,
            chat_logs=self._chat_logs,
            openuserid=self._openuserid,
            media_fetcher=self._media_fetcher,
        )
        run = asyncio.create_task(self._run(ctx), name=f"task-{task.id}")
        self._running[task.id] = (run, ctx)
        run.add_done_callback(functools.partial(self._on_task_done, task.id))

    def _on_task_done(self, task_id: int, run: asyncio.Task[None]) -> None:
        self._running.pop(task_id, None)
        if not run.cancelled() and run.exception() is not None:
            self._log.error("task_runner_crashed", task_id=task_id)
        # 槽位腾出来了，两条车道都叫醒：normal 的上限是「总额 - fast」，fast 任务结束也放宽它。
        for event in self._wake.values():
            event.set()

    # ---- 执行 -------------------------------------------------------------

    async def _run(self, ctx: TaskContext) -> None:
        try:
            async with self._factory() as session:
                await tasks.start(session, ctx.task.id)
                await session.commit()
            handler = self._handlers.get(ctx.task.kind)
            if handler is None:
                ctx.log.warning("task_unknown_kind", kind=ctx.task.kind)
                await self._fail(ctx, "unknown_kind", f"未知任务种类 {ctx.task.kind}")
                return
            await handler.run(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 处理器出什么事都只算这一个任务失败
            ctx.log.exception("task_handler_crashed", kind=ctx.task.kind)
            await self._fail(ctx, type(exc).__name__, f"{type(exc).__name__}: {exc}")

    async def _fail(self, ctx: TaskContext, code: str, message: str) -> None:
        """把失败落回 tasks；流已经开了就补一个终态，别让用户那边一直转圈。"""
        try:
            async with self._factory() as session:
                row = await tasks.get(session, ctx.task.id)
                if row is not None and row.status in tasks.ACTIVE:
                    await tasks.finish(
                        session,
                        ctx.task.id,
                        status="failed",
                        error_code=code,
                        error_message=message,
                    )
                stream = await streams.get(session, ctx.task.id)
                if stream is not None and not stream.is_complete:
                    await streams.complete(
                        session, ctx.task.id, final_text=msg("relay_error", ctx.locale, relay="?")
                    )
                await session.commit()
        except Exception:  # noqa: BLE001 记不下失败也不能再抛一层
            ctx.log.exception("task_failure_record_failed")

    # ---- 心跳 / 取消 / 配置 ------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        # 不看 self._stop：优雅退出期间在途任务还在跑，心跳断了会被巡检当成失联进程。
        while True:
            try:
                async with self._factory() as session:
                    drain = await instances.heartbeat(
                        session, self.instance_id, running=len(self._running)
                    )
                    await session.commit()
                if drain and not self.draining:
                    self.draining = True
                    self._log.info("drain_requested")
                for _run, ctx in list(self._running.values()):
                    await ctx.heartbeat()
            except Exception:  # noqa: BLE001 心跳失败下一轮再来
                self._log.exception("heartbeat_failed")
            if self.draining and not self._running:
                self.request_stop("drained")
                return
            await asyncio.sleep(self.heartbeat_seconds)

    async def _cancel_loop(self) -> None:
        while not self._stop.is_set():
            payloads = await self._listener.wait("task_cancel", timeout=self._poll)
            for payload in payloads:
                try:
                    await self._apply_cancel(int(payload.get("task_id", -1)))
                except Exception:  # noqa: BLE001 取消传导失败还有心跳兜底
                    self._log.exception("task_cancel_failed", payload=payload)

    async def _apply_cancel(self, task_id: int) -> None:
        entry = self._running.get(task_id)
        if entry is None:
            return
        ctx = entry[1]
        async with self._factory() as session:
            # 通知里只有 task_id，原因要回表拿；顺带确认这一行确实还处于「被请求取消」的状态，
            # 别把一个刚好写完 finish、只是协程还没退出的任务误标成取消。
            state, reason = await tasks.heartbeat(session, task_id)
            await session.commit()
        if state is tasks.Heartbeat.CANCELLED:
            ctx.request_cancel(reason or "cancelled")

    async def _config_loop(self) -> None:
        while not self._stop.is_set():
            if await self._listener.wait("config_changed", timeout=self._poll):
                self._store.invalidate()
