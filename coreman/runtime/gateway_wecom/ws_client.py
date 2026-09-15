"""企业微信智能机器人 WebSocket 长连接客户端（移植 平台协议 §3.1，常量见 spec §17.2）。

一条连接 = 一个机器人（`bot_id` + `secret` 订阅）。除了收发帧，这个类还扛着四件线上被反复
验证过的事：

1. **订阅失败必须走退避**：TCP/WSS 几乎总能连上，密钥配错时如果把「连上」当成功就会退化成
   零间隔重连风暴，所以退避延迟只在订阅成功后才重置为 base。
2. **看门狗**：应用层 ping 是 fire-and-forget，链路假死（TCP ESTABLISHED 但不通）时心跳察觉
   不到，只能靠「多久没收到任何入站帧」来判定并主动断开。
3. **踢线熔断**：同一 `bot_id` 被别的实例抢走时企微会发 `disconnected_event` 把旧连接踢掉，
   两个实例会互相踢成死循环；窗口内踢线次数到上限就熔断，宁可不连也不参与抢连。
4. **密钥只在内存里**：`secret` 不进任何日志——订阅帧整体都不打，日志只带 `bot_key`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from coreman.core.logging import get_logger

WS_URL = "wss://openws.work.weixin.qq.com"

# websockets 自己的 DEBUG 日志会把每一帧的首尾原样打出来，而订阅帧的尾巴恰好是 secret。
# 给库一个钉死在 INFO 的 logger：整个进程开 LOG_LEVEL=DEBUG 时，密钥也不会顺着帧日志漏出去
# （代价是没有库级帧跟踪，排查协议问题请看本模块自己的事件日志）。
_WS_LIB_LOGGER = logging.getLogger("coreman.wecom.ws")
_WS_LIB_LOGGER.setLevel(logging.INFO)

FrameHandler = Callable[[dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[str], Awaitable[None]]
ErrcodeHandler = Callable[[str, int, str], Awaitable[None]]
ConnectFactory = Callable[[str], AbstractAsyncContextManager[ClientConnection]]


@dataclass(frozen=True)
class WsConfig:
    """长连接的全部时间常量（spec §17.2 默认值；测试可整体缩短）。"""

    url: str = WS_URL
    ping_interval: float = 30.0
    watchdog_interval: float = 15.0
    idle_timeout: float = 90.0
    reconnect_base: float = 2.0
    reconnect_max: float = 60.0
    subscribe_timeout: float = 10.0
    kick_limit: int = 10
    kick_window: float = 300.0
    close_timeout: float = 10.0


# B008：默认参数里不能直接 `WsConfig()`，用模块级单例（frozen 且无可变字段，共享安全）。
DEFAULT_WS_CONFIG = WsConfig()


class SubscribeRejected(Exception):
    """订阅被企微拒绝（典型 `853000 invalid bot_id or secret`）。"""

    def __init__(self, errcode: int, errmsg: str = "") -> None:
        super().__init__(f"订阅被拒：errcode={errcode} errmsg={errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class _Reconnect(Exception):
    """内部信号：当前连接不可用，退出内层任务走外层退避重连。"""


class DeliveryRejected(RuntimeError):
    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"errcode={errcode} {errmsg}")
        self.errcode = errcode


class DeliveryUncertain(RuntimeError):
    """发送后未收到确认，不能安全地声称已送达或盲目重投。"""


def _as_int(value: Any, default: int) -> int:
    """errcode 一律按 int 读，但对端给什么都不能把收帧循环顶翻。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_connect(config: WsConfig) -> ConnectFactory:
    """默认连接工厂：自管心跳（`ping_interval=None`），并禁用系统/集群透明代理。"""

    def factory(url: str) -> AbstractAsyncContextManager[ClientConnection]:
        return ws_connect(
            url,
            ping_interval=None,
            close_timeout=config.close_timeout,
            proxy=None,
            logger=_WS_LIB_LOGGER,
        )

    return factory


class WeComWsClient:
    """一个机器人的企微长连接。

    Attributes:
        state: `disconnected` / `connecting` / `subscribed` / `kicked` / `auth_failed`
        connections: 订阅成功的累计次数（重连计数，测试与运维都看它）
        kick_times: 熔断窗口内的踢线时刻
        fused: 踢线熔断已触发，不再重连
        secret: 订阅密钥；外部改了它，下一次重连就用新值（换密钥无需重建对象）
    """

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        bot_key: str,
        on_frame: FrameHandler,
        on_state: StateHandler | None = None,
        on_errcode: ErrcodeHandler | None = None,
        config: WsConfig = DEFAULT_WS_CONFIG,
        connect: ConnectFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.bot_key = bot_key
        self.on_frame = on_frame
        self.on_state = on_state
        self.on_errcode = on_errcode
        self.config = config
        self.clock = clock
        self.state = "disconnected"
        self.connections = 0
        self.kick_times: list[float] = []
        self.fused = False
        self._connect = connect or _default_connect(config)
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # 停止信号做成 Event 而不是靠 cancel：退避 sleep 能立刻醒来，
        # 不必在 `async with connect(...)` 半途被取消（那样连接来不及正常关闭）。
        self._stop = asyncio.Event()
        self._last_recv = 0.0
        self._log = get_logger(__name__)
        self._acks: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ---- 对外 ----

    async def start(self) -> None:
        """拉起后台连接循环并立即返回。"""
        if self._task is not None:
            return
        self._stopping = False
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"wecom-ws-{self.bot_key}")

    async def stop(self) -> None:
        """停止连接循环（幂等）。先关连接让循环自己收尾，兜底才取消任务。"""
        self._stopping = True
        self._stop.set()
        task, self._task = self._task, None
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), self.config.close_timeout)
        if task is not None:
            if not task.done():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), self.config.close_timeout)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not self.fused:
            await self._set_state("disconnected")

    async def send(self, data: dict[str, Any]) -> None:
        """发一帧 JSON；未订阅成功时拒绝发送（上层据此判断要不要丢弃这次推送）。"""
        ws = self._ws
        if self.state != "subscribed" or ws is None:
            raise RuntimeError("WebSocket 未连接")
        await ws.send(json.dumps(data, ensure_ascii=False))

    async def send_confirmed(self, data: dict[str, Any], *, timeout: float = 10.0) -> None:  # noqa: ASYNC109 bounded platform acknowledgement
        """先登记回执再发送；接收循环只唤醒 future，不等待发送方的数据库锁。"""
        req_id = str(data["headers"]["req_id"])
        if req_id in self._acks:
            raise RuntimeError("同一 req_id 正在等待确认")
        future = asyncio.get_running_loop().create_future()
        self._acks[req_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send(data)
                ack = await future
            code = _as_int(ack.get("errcode"), -1)
            if code:
                raise DeliveryRejected(code, str(ack.get("errmsg", ""))[:300])
        except (TimeoutError, WebSocketException, OSError) as exc:
            raise DeliveryUncertain("delivery_unknown: 未收到平台送达确认") from exc
        finally:
            self._acks.pop(req_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    # ---- 连接循环 ----

    async def _run(self) -> None:
        delay = self.config.reconnect_base
        while not self._stopping and not self.fused:
            await self._set_state("connecting")
            # 订阅被拒要停在 auth_failed 上（运维一眼看出是密钥问题），别被 disconnected 盖掉。
            failure_state = "disconnected"
            try:
                async with self._connect(self.config.url) as ws:
                    self._ws = ws
                    await self._subscribe(ws)
                    self.connections += 1
                    delay = self.config.reconnect_base
                    self._last_recv = self.clock()
                    await self._set_state("subscribed")
                    await self._serve(ws)
            except SubscribeRejected as exc:
                failure_state = "auth_failed"
                self._log.warning(
                    "wecom_subscribe_rejected", bot_key=self.bot_key, errcode=exc.errcode
                )
            except (OSError, WebSocketException, _Reconnect) as exc:
                # TimeoutError 是 OSError 的子类，订阅超时也落在这里（按断线处理并退避）。
                self._log.info(
                    "wecom_ws_disconnected", bot_key=self.bot_key, reason=type(exc).__name__
                )
            except Exception:  # noqa: BLE001 循环死了 state 会永远停在 connecting，比重连更糟
                self._log.exception("wecom_ws_loop_error", bot_key=self.bot_key)
            finally:
                self._ws = None
                for waiter in self._acks.values():
                    if not waiter.done():
                        waiter.set_exception(
                            DeliveryUncertain("delivery_unknown: connection closed")
                        )
            if self._stopping or self.fused:
                break
            await self._set_state(failure_state)
            await self._wait_backoff(delay)
            delay = min(delay * 2, self.config.reconnect_max)
        if not self.fused:
            await self._set_state("disconnected")

    async def _serve(self, ws: ClientConnection) -> None:
        """心跳 / 看门狗 / 收帧三任务并跑，任一退出就收掉其余，把异常抛给外层重连。

        另加一个只等停止信号的任务：`stop()` 有可能正卡在建连/订阅途中被调用（那会儿 `_ws`
        还没挂上，它关不到任何东西），有这一路才能让刚建好的连接立刻正常收尾，
        而不是干等到 `stop()` 的取消兜底超时。
        """
        stopper = asyncio.create_task(self._stop.wait())
        tasks = [
            asyncio.create_task(coro)
            for coro in (self._heartbeat(ws), self._watchdog(ws), self._receive(ws))
        ]
        waiting: list[asyncio.Task[Any]] = [*tasks, stopper]
        try:
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopper and not task.cancelled():
                    task.result()
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

    async def _wait_backoff(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), delay)

    # ---- 三个内部任务 ----

    async def _subscribe(self, ws: ClientConnection) -> None:
        # 订阅帧带 secret，整帧都不进日志。
        await self._send_frame(
            ws,
            {
                "cmd": "aibot_subscribe",
                "headers": {"req_id": str(uuid.uuid4())},
                "body": {"bot_id": self.bot_id, "secret": self.secret},
            },
        )
        raw = await asyncio.wait_for(ws.recv(), self.config.subscribe_timeout)
        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise SubscribeRejected(-1, "订阅响应不是合法 JSON") from exc
        if not isinstance(reply, dict):
            raise SubscribeRejected(-1, "订阅响应不是对象")
        errcode = _as_int(reply.get("errcode"), -1)
        if errcode != 0:
            raise SubscribeRejected(errcode, str(reply.get("errmsg", "")))

    async def _heartbeat(self, ws: ClientConnection) -> None:
        while True:
            await self._send_frame(ws, {"cmd": "ping", "headers": {"req_id": str(uuid.uuid4())}})
            await asyncio.sleep(self.config.ping_interval)

    async def _watchdog(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.config.watchdog_interval)
            idle = self.clock() - self._last_recv
            if idle > self.config.idle_timeout:
                self._log.warning(
                    "wecom_ws_idle_timeout", bot_key=self.bot_key, idle=round(idle, 1)
                )
                with contextlib.suppress(Exception):
                    await ws.close()
                raise _Reconnect

    async def _receive(self, ws: ClientConnection) -> None:
        async for raw in ws:
            self._last_recv = self.clock()
            try:
                frame = json.loads(raw)
            except ValueError:
                self._log.warning("wecom_ws_bad_frame", bot_key=self.bot_key)
                continue
            if not isinstance(frame, dict):
                continue
            cmd = frame.get("cmd", "")
            if cmd == "" and "errcode" in frame:
                await self._handle_ack(frame)
                continue
            if cmd == "aibot_event_callback" and self._event_type(frame) == "disconnected_event":
                self._record_kick()
                if self.fused:
                    await self._set_state("kicked")
                    return
                raise _Reconnect
            await self._emit("on_frame", self.on_frame(frame))
        raise _Reconnect

    # ---- 细节 ----

    async def _handle_ack(self, frame: dict[str, Any]) -> None:
        """ping / 推送的响应包：errcode 0 忽略，非 0 交给上层去反查 req_id 的推送上下文。"""
        req_id = str((frame.get("headers") or {}).get("req_id", ""))
        waiter = self._acks.get(req_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(frame)
            return
        errcode = _as_int(frame.get("errcode"), 0)
        if errcode:
            from coreman.core.observability.metrics import WECOM_ERRORS

            code = str(errcode) if errcode in {40014, 42001, 846607, 6000, 6001, 6002} else "other"
            WECOM_ERRORS.labels(code).inc()
        if errcode == 0 or self.on_errcode is None:
            return
        req_id = str((frame.get("headers") or {}).get("req_id", ""))
        errmsg = str(frame.get("errmsg", ""))
        await self._emit("on_errcode", self.on_errcode(req_id, errcode, errmsg))

    @staticmethod
    def _event_type(frame: dict[str, Any]) -> str:
        body = frame.get("body")
        event = body.get("event") if isinstance(body, dict) else None
        return str(event.get("eventtype", "")) if isinstance(event, dict) else ""

    def _record_kick(self) -> None:
        now = self.clock()
        self.kick_times = [t for t in self.kick_times if now - t < self.config.kick_window] + [now]
        if len(self.kick_times) >= self.config.kick_limit:
            self.fused = True
            self._log.error(
                "wecom_ws_kick_fused",
                bot_key=self.bot_key,
                kicks=len(self.kick_times),
                window=self.config.kick_window,
            )
        else:
            self._log.warning(
                "wecom_ws_kicked",
                bot_key=self.bot_key,
                kicks=len(self.kick_times),
                limit=self.config.kick_limit,
            )

    async def _send_frame(self, ws: ClientConnection, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        self._log.info("wecom_ws_state", bot_key=self.bot_key, state=state)
        if self.on_state is not None:
            await self._emit("on_state", self.on_state(state))

    async def _emit(self, name: str, awaitable: Awaitable[None]) -> None:
        """回调是上层的事：它抛异常不该把长连接带下来（否则一个处理 bug = 无限重连）。"""
        try:
            await awaitable
        except Exception:  # noqa: BLE001 回调里出什么事都只记一笔，连接照常跑
            self._log.exception("wecom_ws_callback_failed", bot_key=self.bot_key, callback=name)
