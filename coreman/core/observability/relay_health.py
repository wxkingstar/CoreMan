"""两小时未上报时仅探测 /health；不发推理请求或刷新模型目录。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import RelayServer
from coreman.core.relay.client import relay_base_url
from coreman.core.relay.safe_transport import RegisteredTransport


async def read_health(relay: RelayServer) -> tuple[str, int]:
    if relay.runtime_node_id:
        from coreman.core.relay.client import RelayClient

        runtime_client = RelayClient(relay.relay_url)
        try:
            health = await runtime_client.health()
            return health.status, health.latency_ms
        finally:
            await runtime_client.aclose()
    started = time.monotonic()
    status = "down"
    try:
        async with (
            asyncio.timeout(5),
            httpx.AsyncClient(
                transport=RegisteredTransport(relay.host, relay.clawrelay_port),
                base_url=relay_base_url(relay.host, relay.clawrelay_port),
                timeout=5,
                trust_env=False,
                follow_redirects=False,
            ) as client,
        ):
            async with client.stream("GET", "/health") as response:
                if response.status_code in {401, 403}:
                    return "auth_fail", int((time.monotonic() - started) * 1000)
                if response.status_code != 200:
                    return "down", int((time.monotonic() - started) * 1000)
                body = bytearray()
                async for part in response.aiter_bytes(8192):
                    body.extend(part)
                    if len(body) > 65536:
                        return "down", int((time.monotonic() - started) * 1000)
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get("status") == "healthy":
                    status = "healthy"
    except (TimeoutError, httpx.TimeoutException):
        status = "timeout"
    except (httpx.HTTPError, ValueError, OSError):
        pass
    return status, int((time.monotonic() - started) * 1000)


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    cutoff = now - timedelta(hours=2)
    stale = or_(RelayServer.health_checked_at.is_(None), RelayServer.health_checked_at < cutoff)
    async with factory() as session:
        candidates = list(
            await session.scalars(
                select(RelayServer.id)
                .where(RelayServer.is_active, stale)
                .order_by(RelayServer.health_checked_at.asc().nulls_first())
                .limit(10)
            )
        )
    changed = 0
    for identity in candidates:
        async with factory() as session:
            if not await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"relay-health:{identity}"},
            ):
                continue
            relay = await session.scalar(
                select(RelayServer).where(RelayServer.id == identity, RelayServer.is_active, stale)
            )
            if relay is None:
                continue
            status, latency = await read_health(relay)
            # 上报或管理员改配置比探测更晚时，不用旧快照覆盖新值。
            result = await session.execute(
                update(RelayServer)
                .where(
                    RelayServer.id == relay.id,
                    RelayServer.version == relay.version,
                    RelayServer.health_checked_at.is_not_distinct_from(relay.health_checked_at),
                    RelayServer.is_active,
                )
                .values(
                    health_status=status,
                    health_detail=None,
                    health_checked_at=now,
                    health_latency_ms=latency,
                    health_fail_count=0
                    if status == "healthy"
                    else RelayServer.health_fail_count + 1,
                )
                .returning(RelayServer.id)
            )
            changed += len(result.all())
            await session.commit()
    return changed
