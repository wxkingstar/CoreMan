"""探测一台 relay：写健康列，健康时预填模型目录（spec §5.3「首次注册预填」）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ModelCatalog, RelayServer
from coreman.core.logging import get_logger
from coreman.core.relay.client import RelayClient, RelayError, RelayHealth, relay_base_url
from coreman.core.relay.safe_transport import RegisteredTransport

log = get_logger(__name__)


def make_client(relay: RelayServer) -> RelayClient:
    """按 relay 的 host + clawrelay_port 建客户端。

    模块级函数：测试用 monkeypatch 替换它来免网络，调用方必须走 `probe.make_client(...)`。
    """
    if relay.runtime_node_id:
        return RelayClient(relay.relay_url)
    base = relay_base_url(relay.host, relay.clawrelay_port)
    return RelayClient(base, transport=RegisteredTransport(relay.host, relay.clawrelay_port))


async def probe_relay(
    session: AsyncSession, relay: RelayServer, client: RelayClient
) -> tuple[RelayHealth, list[str]]:
    """探一次 /health（健康时再拉 /v1/models 预填目录），只写库不提交，由调用方提交。

    Args:
        session: 数据库会话（不 commit）
        relay: 被探测的 relay 行（健康列就地更新）
        client: 该 relay 的客户端（不由本函数关闭）

    Returns:
        (health, added_models)：本次探测结果与新写入目录的模型名
    """
    health = await client.health()
    values = dict(
        health_status=health.status,
        health_detail=health.detail,
        health_checked_at=datetime.now(UTC),
        health_latency_ms=health.latency_ms,
        health_fail_count=0 if health.status == "healthy" else RelayServer.health_fail_count + 1,
    )
    if health.status == "healthy":
        values.update(relay_version=health.version, relay_mode=health.mode)
    # 批量列更新不触发 ORM 配置 version，后台遥测不会打断用户保存表单。
    await session.execute(update(RelayServer).where(RelayServer.id == relay.id).values(**values))
    if health.status != "healthy":
        log.warning("relay_unhealthy", relay=relay.name, status=health.status)
        return health, []
    try:
        models = await client.models()
    except RelayError as exc:
        log.warning("relay_models_failed", relay=relay.name, error=str(exc))
        return health, []
    existing = {
        r.model
        for r in (
            await session.execute(
                select(ModelCatalog).where(ModelCatalog.provider == relay.model_provider)
            )
        ).scalars()
    }
    added: list[str] = []
    for model in models:
        if model in existing:
            continue
        # 该 provider 原本一行都没有时，第一条预填的模型顺带当默认，否则不动既有默认。
        session.add(
            ModelCatalog(
                provider=relay.model_provider,
                model=model,
                is_default=not existing and not added,
                sort_order=0,
            )
        )
        added.append(model)
    await session.flush()
    log.info("relay_probed", relay=relay.name, latency_ms=health.latency_ms, added=len(added))
    return health, added
