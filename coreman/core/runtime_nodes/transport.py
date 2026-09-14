"""Reverse HTTP via PostgreSQL: works across API replicas and worker processes.

Only encrypted, bounded transient envelopes are persisted. The consumer removes
chunks as it reads them; deadlines and consumer leases prevent abandoned work.
No automatic execution retry: a lost caller must not repeat side effects.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import RuntimeCall, RuntimeChunk, RuntimeNode

MAX_REQUEST = 64 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
CHUNK_SIZE = 48 * 1024
ONLINE_SECONDS = 45
LEASE_SECONDS = 40
TERMINAL = {"done", "failed", "cancelled"}


def now() -> datetime:
    return datetime.now(UTC)


def online(node: RuntimeNode) -> bool:
    return bool(node.heartbeat_at and node.heartbeat_at > now() - timedelta(seconds=ONLINE_SECONDS))


def envelope_aad(call_id: uuid.UUID) -> str:
    return f"runtime:{call_id}:request"


def chunk_aad(call_id: uuid.UUID, seq: int) -> str:
    return f"runtime:{call_id}:chunk:{seq}"


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
        self.engine = None
        if factory is None:
            cfg = get_settings()
            self.engine = create_async_engine(
                cfg.database_url, poolclass=NullPool, hide_parameters=True
            )
            factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.factory = factory
        self.cipher = cipher or Cipher(get_settings().master_key_bytes)

    async def aclose(self) -> None:
        if self.engine:
            await self.engine.dispose()

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
            await session.commit()
        timeout = request.extensions.get("timeout", {})
        stream = ReverseStream(self, call_id, request, float(timeout.get("read") or 120))
        accepted = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout.get("connect") or 10)
        try:
            while True:
                async with asyncio.timeout_at(deadline):
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
                        await asyncio.sleep(0.15)
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
        last_data = asyncio.get_running_loop().time()
        while not self.closed:
            row, chunks = await self.read_batch()
            for chunk in chunks:
                last_data = asyncio.get_running_loop().time()
                yield chunk
            if row.status == "done" and not chunks:
                return
            if row.status in {"failed", "cancelled"}:
                raise httpx.ReadError(row.error or "运行时任务已终止", request=self.request)
            if asyncio.get_running_loop().time() - last_data > self.read_timeout:
                raise httpx.ReadTimeout("运行时响应超时", request=self.request)
            if not chunks:
                await asyncio.sleep(0.1)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        async with self.transport.factory() as session:
            await session.execute(
                update(RuntimeCall)
                .where(RuntimeCall.id == self.call_id, RuntimeCall.status.not_in(TERMINAL))
                .values(status="cancelled", request_enc="")
            )
            await session.execute(delete(RuntimeChunk).where(RuntimeChunk.call_id == self.call_id))
            await session.commit()


def runtime_transport(base_url: str) -> ReverseTransport | None:
    url = httpx.URL(base_url)
    if not url.host.endswith(".runtime"):
        return None
    return ReverseTransport(uuid.UUID(url.host.removesuffix(".runtime")), url.path.strip("/"))
