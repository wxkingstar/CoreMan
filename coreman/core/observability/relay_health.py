"""两小时未上报时仅探测 /health；不发推理请求或刷新模型目录。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import RelayServer
from coreman.core.relay.client import RelayClient


async def read_health(relay: RelayServer) -> tuple[str, int]:
    """经实例所属节点的反向通道探测 /health。"""
    client = RelayClient.for_relay(relay)
    try:
        health = await client.health()
        return health.status, health.latency_ms
    finally:
        await client.aclose()


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    cutoff = now - timedelta(hours=2)
    stale = or_(RelayServer.health_checked_at.is_(None), RelayServer.health_checked_at < cutoff)
    usable = RelayServer.is_active & RelayServer.runtime_node_id.is_not(None)
    async with factory() as session:
        candidates = list(
            await session.scalars(
                select(RelayServer.id)
                .where(usable, stale)
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
                select(RelayServer).where(RelayServer.id == identity, usable, stale)
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
