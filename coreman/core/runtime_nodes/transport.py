"""Reverse HTTP via PostgreSQL: works across API replicas and worker processes.

Only encrypted, bounded transient envelopes are persisted. The consumer removes
chunks as it reads them; deadlines and consumer leases prevent abandoned work.
No automatic execution retry: a lost caller must not repeat side effects.

进程启动时用 `configure(session_factory, listener)` 注册本进程的有池会话工厂与 LISTEN 连接：
写入方在同一事务里 pg_notify，消费端按 call_id / node_id 分发唤醒，1 秒兜底轮询。
未注册时回退到一个小连接池，并以较短间隔轮询（没有通知可等）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
import weakref
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coreman.core.bus.notify import (
    RUNTIME_CALL_CHANNEL,
    RUNTIME_NODE_CHANNEL,
    Listener,
    notify,
)
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import RuntimeCall, RuntimeChunk, RuntimeNode
from coreman.core.db.session import make_session_factory

MAX_REQUEST = 64 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
CHUNK_SIZE = 48 * 1024
ONLINE_SECONDS = 45
LEASE_SECONDS = 40
TERMINAL = {"done", "failed", "cancelled"}
# 有 LISTEN 时通知是主路径，轮询只兜底丢通知；没有监听连接时只能靠轮询。
FALLBACK_POLL_SECONDS = 1.0
UNLISTENED_POLL_SECONDS = 0.25


def now() -> datetime:
    return datetime.now(UTC)


def online(node: RuntimeNode) -> bool:
    return bool(node.heartbeat_at and node.heartbeat_at > now() - timedelta(seconds=ONLINE_SECONDS))


def envelope_aad(call_id: uuid.UUID) -> str:
    return f"runtime:{call_id}:request"


def chunk_aad(call_id: uuid.UUID, seq: int) -> str:
    return f"runtime:{call_id}:chunk:{seq}"


# ---- 进程级配置 -------------------------------------------------------------


@dataclass
class _Config:
    factory: async_sessionmaker[AsyncSession] | None = None
    listener: Listener | None = None


_config = _Config()
# 未配置时的回退连接池按事件循环各建一个：asyncpg 连接不能跨事件循环复用。
_fallback: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
] = weakref.WeakKeyDictionary()


def configure(
    session_factory: async_sessionmaker[AsyncSession] | None,
    listener: Listener | None = None,
) -> None:
    """注册本进程的会话工厂与 LISTEN 连接；传 None 恢复未配置状态（进程退出时调用）。

    listener 须已 LISTEN `RUNTIME_CHANNELS`（或其中本进程需要的通道），启停由调用方负责；
    注册后本模块常驻消费这两个通道的载荷。
    """
    for task in _dispatchers:
        task.cancel()
    _dispatchers.clear()
    _CALLS.waiters.clear()
    _NODES.waiters.clear()
    _config.factory, _config.listener = session_factory, listener
    if listener is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环时在首次订阅时再启动分发协程
        _ensure_dispatchers()


def session_factory() -> async_sessionmaker[AsyncSession]:
    """本进程共用的会话工厂；未配置时用一个小连接池（不是 NullPool）。"""
    if _config.factory is not None:
        return _config.factory
    loop = asyncio.get_running_loop()
    cached = _fallback.get(loop)
    if cached is None:
        engine = create_async_engine(
            get_settings().database_url,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        cached = (engine, make_session_factory(engine))
        _fallback[loop] = cached
    return cached[1]


def poll_seconds() -> float:
    listener = _config.listener
    if listener is not None and listener.connected:
        return FALLBACK_POLL_SECONDS
    return UNLISTENED_POLL_SECONDS


# ---- 通知分发 ---------------------------------------------------------------


class _Hub:
    """把一个通道上的通知按载荷里的某个键分发给订阅者。"""

    def __init__(self, channel: str, key: str) -> None:
        self.channel, self.key = channel, key
        self.waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    def subscribe(self, value: str) -> asyncio.Event:
        event = asyncio.Event()
        self.waiters[value].add(event)
        _ensure_dispatchers()
        return event

    def unsubscribe(self, value: str, event: asyncio.Event) -> None:
        events = self.waiters.get(value)
        if events is not None:
            events.discard(event)
            if not events:
                del self.waiters[value]

    def dispatch(self, payloads: list[dict[str, object]]) -> None:
        for payload in payloads:
            for event in tuple(self.waiters.get(str(payload.get(self.key)), ())):
                event.set()


_CALLS = _Hub(RUNTIME_CALL_CHANNEL, "call_id")
_NODES = _Hub(RUNTIME_NODE_CHANNEL, "node_id")
_dispatchers: list[asyncio.Task[None]] = []


async def _dispatch(listener: Listener, hub: _Hub) -> None:
    # 常驻排空：没有本地订阅者时也要取走载荷，否则别的副本上调用的通知会在 Listener 里无限积压。
    while True:
        hub.dispatch(await listener.wait(hub.channel, timeout=FALLBACK_POLL_SECONDS))


def _ensure_dispatchers() -> None:
    listener = _config.listener
    if listener is None:
        return
    loop = asyncio.get_running_loop()
    if _dispatchers and all(not t.done() and t.get_loop() is loop for t in _dispatchers):
        return
    for task in _dispatchers:
        task.cancel()
    _dispatchers[:] = [
        loop.create_task(_dispatch(listener, hub), name=f"runtime-wakeups-{hub.channel}")
        for hub in (_CALLS, _NODES)
    ]


async def wait_event(event: asyncio.Event, timeout: float) -> bool:  # noqa: ASYNC109
    """等通知或到点兜底；返回是否被唤醒。调用方在读库之前 clear，读库期间的通知不会丢。"""
    if timeout <= 0:
        return event.is_set()
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        return False
    return True


def subscribe_call(call_id: uuid.UUID) -> asyncio.Event:
    return _CALLS.subscribe(str(call_id))


def unsubscribe_call(call_id: uuid.UUID, event: asyncio.Event) -> None:
    _CALLS.unsubscribe(str(call_id), event)


def subscribe_node(node_id: uuid.UUID) -> asyncio.Event:
    return _NODES.subscribe(str(node_id))


def unsubscribe_node(node_id: uuid.UUID, event: asyncio.Event) -> None:
    _NODES.unsubscribe(str(node_id), event)


async def notify_call(session: AsyncSession, call_id: uuid.UUID, node_id: uuid.UUID) -> None:
    """调用状态变化（被领取、写入响应帧、终态）：与写操作同事务，提交后送达。"""
    await notify(session, RUNTIME_CALL_CHANNEL, {"call_id": str(call_id), "node_id": str(node_id)})


async def notify_node(
    session: AsyncSession, node_id: uuid.UUID, call_id: uuid.UUID | None = None
) -> None:
    """节点有新命令或在途调用被取消：唤醒该节点挂起的长轮询。"""
    payload = {"node_id": str(node_id), "call_id": str(call_id) if call_id else None}
    await notify(session, RUNTIME_NODE_CHANNEL, payload)


# ---- 反向通道 -----------------------------------------------------------------


class ReverseTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        node_id: uuid.UUID,
        provider: str,
        *,
        factory: async_sessionmaker[AsyncSession] | None = None,
        cipher: Cipher | None = None,
    ) -> None:
        self.node_id, self.provider = node_id, provider
        self._factory, self._cipher = factory, cipher

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        # 延迟到首次使用：构造时可能还没有事件循环，也可能尚未 configure。
        return self._factory or session_factory()

    @property
    def cipher(self) -> Cipher:
        if self._cipher is None:
            self._cipher = Cipher(get_settings().master_key_bytes)
        return self._cipher

    async def aclose(self) -> None:
        """会话工厂由进程共享，这里没有需要释放的连接。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        if len(raw) > MAX_REQUEST:
            raise httpx.RequestError("运行时请求超过 64 MiB", request=request)
        path = request.url.path
        prefix = f"/{self.provider}"
        if path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        if self.provider not in {"claude", "codex"}:
            raise httpx.RequestError("运行时 AI 类型无效", request=request)
        call_id = uuid.uuid4()
        payload = json.dumps(
            {
                "method": request.method,
                "path": path,
                "body": base64.b64encode(raw).decode(),
            }
        )
        started = now()
        async with self.factory() as session:
            node = await session.get(RuntimeNode, self.node_id)
            if not node or not node.is_active or not online(node):
                return httpx.Response(503, text="运行时离线或未启用")
            if node.draining and request.method == "POST":
                return httpx.Response(503, text="运行时正在排空任务")
            pending = await session.scalar(
                select(func.count())
                .select_from(RuntimeCall)
                .where(RuntimeCall.node_id == self.node_id, RuntimeCall.status.not_in(TERMINAL))
            )
            if pending and pending >= 128:
                return httpx.Response(429, text="运行时等待任务过多")
            session.add(
                RuntimeCall(
                    id=call_id,
                    node_id=self.node_id,
                    provider=self.provider,
                    request_enc=self.cipher.encrypt(payload, envelope_aad(call_id)),
                    deadline=started + timedelta(hours=2),
                    consumer_at=started,
                )
            )
            await notify_node(session, self.node_id, call_id)
            await session.commit()
        timeout = request.extensions.get("timeout", {})
        stream = ReverseStream(self, call_id, request, float(timeout.get("read") or 120))
        accepted = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout.get("connect") or 10)
        try:
            while True:
                async with asyncio.timeout_at(deadline):
                    stream.wake.clear()
                    row, _ = await stream.read_batch(include_chunks=False)
                    if row.status_code is not None:
                        return httpx.Response(
                            row.status_code,
                            headers={"Content-Type": row.content_type or "application/json"},
                            stream=stream,
                        )
                    if row.status in TERMINAL:
                        raise httpx.ConnectError(row.error or "运行时请求终止", request=request)
                    if row.status == "running" and not accepted:
                        accepted = True
                        deadline = loop.time() + stream.read_timeout
                    else:
                        await wait_event(stream.wake, poll_seconds())
        except TimeoutError as exc:
            await stream.aclose()
            if accepted:
                raise httpx.ReadTimeout("运行时已接收，但响应头超时", request=request) from exc
            raise httpx.ConnectTimeout("运行时未及时接收请求", request=request) from exc
        except BaseException:
            await stream.aclose()
            raise


class ReverseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        transport: ReverseTransport,
        call_id: uuid.UUID,
        request: httpx.Request,
        read_timeout: float,
    ) -> None:
        self.transport, self.call_id, self.request = transport, call_id, request
        self.read_timeout, self.closed = read_timeout, False
        # 在读库之前订阅：节点在两次读取之间写入的帧一定能唤醒本流。
        self.wake = subscribe_call(call_id)

    async def read_batch(self, *, include_chunks: bool = True) -> tuple[RuntimeCall, list[bytes]]:
        async with self.transport.factory() as session:
            row = await session.get(RuntimeCall, self.call_id)
            if row is None or row.deadline <= now():
                raise httpx.ReadError("运行时请求已过期", request=self.request)
            row.consumer_at = now()
            data = []
            if include_chunks:
                chunks = list(
                    await session.scalars(
                        select(RuntimeChunk)
                        .where(RuntimeChunk.call_id == self.call_id)
                        .order_by(RuntimeChunk.seq)
                        .limit(32)
                    )
                )
                for chunk in chunks:
                    data.append(
                        base64.b64decode(
                            self.transport.cipher.decrypt(
                                chunk.data_enc, chunk_aad(self.call_id, chunk.seq)
                            ),
                            validate=True,
                        )
                    )
                    await session.delete(chunk)
            await session.commit()
            return row, data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        last_data = loop.time()
        while not self.closed:
            self.wake.clear()
            row, chunks = await self.read_batch()
            for chunk in chunks:
                last_data = loop.time()
                yield chunk
            if row.status == "done" and not chunks:
                return
            if row.status in {"failed", "cancelled"}:
                raise httpx.ReadError(row.error or "运行时任务已终止", request=self.request)
            idle = loop.time() - last_data
            if idle > self.read_timeout:
                raise httpx.ReadTimeout("运行时响应超时", request=self.request)
            if not chunks:
                await wait_event(self.wake, min(poll_seconds(), self.read_timeout - idle))

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        unsubscribe_call(self.call_id, self.wake)
        async with self.transport.factory() as session:
            cancelled = await session.scalar(
                update(RuntimeCall)
                .where(RuntimeCall.id == self.call_id, RuntimeCall.status.not_in(TERMINAL))
                .values(status="cancelled", request_enc="")
                .returning(RuntimeCall.id)
            )
            await session.execute(delete(RuntimeChunk).where(RuntimeChunk.call_id == self.call_id))
            if cancelled is not None:
                # 节点可能正挂在长轮询上：立刻让它知道这个调用要停。
                await notify_node(session, self.transport.node_id, self.call_id)
            await session.commit()
