"""内部 Prometheus 指标：不含消息、用户、机器人或凭证标签。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from coreman.core.db.models import Bot, BotLease, OutboxItem, RelayServer, Task
from coreman.core.db.models.bus import CONNECTION_STATES

REGISTRY = CollectorRegistry()
TASK_DURATION = Histogram(
    "coreman_task_duration_seconds",
    "Committed task duration",
    ["kind", "status"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 7200),
    registry=REGISTRY,
)
FIRST_BYTE = Histogram(
    "coreman_sse_first_byte_seconds",
    "Time until first relay event",
    ["relay"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
    registry=REGISTRY,
)
OUTBOX_FAILED = Counter(
    "coreman_outbox_failed_total", "Committed permanent delivery failures", registry=REGISTRY
)
TAKEOVERS = Counter(
    "coreman_lease_takeovers_total", "Committed lease acquisitions", ["platform"], registry=REGISTRY
)
WECOM_ERRORS = Counter(
    "coreman_wecom_errcode_total", "Platform errors by bounded code", ["code"], registry=REGISTRY
)
# 白名单拒绝：reason 只有 identity_unknown（平台账号没映射到员工，多为应用未绑定或不在
# 可见范围）与 not_allowed（员工不在名单）两种。成批出现前者往往是配置问题而不是越权。
WHITELIST_DENIED = Counter(
    "coreman_whitelist_denied_total",
    "Messages rejected by a bot allow list",
    ["reason"],
    registry=REGISTRY,
)


def after_commit(session: AsyncSession, action: Callable[[], object]) -> None:
    session.info.setdefault("coreman_metrics", []).append(action)


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    for action in session.info.pop("coreman_metrics", []):
        action()


@event.listens_for(Session, "after_rollback")
def _rolled_back(session: Session) -> None:
    session.info.pop("coreman_metrics", None)


async def render(factory: async_sessionmaker[AsyncSession] | None = None) -> bytes:
    if factory is None:
        return generate_latest(REGISTRY)
    registry = CollectorRegistry()
    async with asyncio.timeout(3), factory() as session:
        await session.execute(text("SET LOCAL statement_timeout = 1500"))
        queued_rows = await session.execute(
            select(Task.lane, func.count()).where(Task.status == "queued").group_by(Task.lane)
        )
        queued = {lane: count for lane, count in queued_rows}
        gauge = Gauge("coreman_tasks_queued", "Queued tasks", ["lane"], registry=registry)
        for lane in ("normal", "fast"):
            gauge.labels(lane).set(queued.get(lane, 0))
        running = await session.scalar(
            select(func.count()).select_from(Task).where(Task.status.in_(("claimed", "running")))
        )
        Gauge("coreman_tasks_running", "Claimed and running tasks", registry=registry).set(
            running or 0
        )
        oldest = await session.scalar(
            select(func.min(OutboxItem.created_at)).where(
                OutboxItem.status.in_(("pending", "sending"))
            )
        )
        lag = max(0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0
        Gauge(
            "coreman_outbox_lag_seconds", "Age of oldest pending delivery", registry=registry
        ).set(lag)
        failed = await session.scalar(
            select(func.count()).select_from(OutboxItem).where(OutboxItem.status == "failed")
        )
        Gauge("coreman_outbox_failed", "Currently failed deliveries", registry=registry).set(
            failed or 0
        )
        state_rows = await session.execute(
            select(BotLease.connection_state, func.count())
            .join(Bot, Bot.id == BotLease.bot_id)
            .where(Bot.enabled)
            .group_by(BotLease.connection_state)
        )
        states = {state: count for state, count in state_rows}
        connections = Gauge(
            "coreman_gateway_connections", "Enabled bot connections", ["state"], registry=registry
        )
        for state in CONNECTION_STATES:
            connections.labels(state).set(states.get(state, 0))
        health = Gauge(
            "coreman_relay_health",
            "Active relay: healthy=1 unhealthy=0 unknown=-1",
            ["relay"],
            registry=registry,
        )
        for identity, state in await session.execute(
            select(RelayServer.id, RelayServer.health_status).where(RelayServer.is_active)
        ):
            health.labels(str(identity)).set(
                1 if state == "healthy" else -1 if state == "unknown" else 0
            )
    return generate_latest(registry) + generate_latest(REGISTRY)
