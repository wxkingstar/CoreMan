"""app_leases 读写：个人助理应用的连接租约。

规则与 `leases`（按 bot）同源：无持有者或持有者心跳过期才能认领，代次每次认领 +1，
只有当前持有者按自己的代次才能释放。函数签名就是 `gateway_common.lease_loop.LeaseStore`，
整个模块直接作为租约扫描器的存储传进去。`key` 一律是 platform_apps.id。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.leases import STALE_AFTER_SECONDS
from coreman.core.db.models import AppLease, PlatformApp
from coreman.core.observability.metrics import TAKEOVERS, after_commit

_ACQUIRE_SQL = text(
    """
UPDATE app_leases SET holder_instance=:me, generation=generation+1,
                      acquired_at=now(), heartbeat_at=now(),
                      drain_requested_by=NULL, drain_requested_at=NULL, released_at=NULL,
                      connection_state='connecting'
WHERE app_id=:app_id
  AND (holder_instance IS NULL OR heartbeat_at < now() - make_interval(secs => :stale))
RETURNING generation
"""
)


def _assistant_app() -> ColumnElement[bool]:
    return and_(PlatformApp.platform == "feishu", PlatformApp.capabilities.contains(["assistant"]))


def _serviceable() -> ColumnElement[bool]:
    """与 `core.assistants.apps.is_assistant_app` 同义的 SQL 条件。"""
    return and_(_assistant_app(), PlatformApp.enabled.is_(True), PlatformApp.app_id.is_not(None))


async def ensure_rows(session: AsyncSession) -> None:
    """为每个助理应用补一行租约（含停用的：停用只由 acquirable 过滤，行本身保留）。"""
    missing = select(PlatformApp.id).where(
        _assistant_app(),
        ~exists(select(AppLease.app_id).where(AppLease.app_id == PlatformApp.id)),
    )
    stmt = insert(AppLease).from_select(["app_id"], missing)
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[AppLease.app_id]))


async def acquirable(session: AsyncSession) -> list[uuid.UUID]:
    stale = func.now() - text(f"interval '{STALE_AFTER_SECONDS} seconds'")
    stmt = (
        select(AppLease.app_id)
        .join(PlatformApp, PlatformApp.id == AppLease.app_id)
        .where(
            _serviceable(),
            (AppLease.holder_instance.is_(None)) | (AppLease.heartbeat_at < stale),
        )
        .order_by(AppLease.app_id)
    )
    return list((await session.execute(stmt)).scalars())


async def acquire(session: AsyncSession, key: uuid.UUID, instance_id: str) -> int | None:
    """抢占式认领；成功返回新的代次，已有活着的持有者返回 None。"""
    row = (
        await session.execute(
            _ACQUIRE_SQL, {"me": instance_id, "app_id": key, "stale": float(STALE_AFTER_SECONDS)}
        )
    ).first()
    if row is None:
        return None
    after_commit(session, lambda: TAKEOVERS.labels("feishu").inc())
    return int(row[0])


async def heartbeat(session: AsyncSession, instance_id: str) -> int:
    rows = (
        await session.execute(
            update(AppLease)
            .where(AppLease.holder_instance == instance_id)
            .values(heartbeat_at=func.now())
            .returning(AppLease.app_id)
        )
    ).all()
    return len(rows)


async def release(
    session: AsyncSession,
    key: uuid.UUID,
    instance_id: str,
    generation: int,
    state: str = "disconnected",
) -> None:
    """只有当前持有者按自己的代次才能释放；顺手清掉排空标记（理由同 `leases.release`）。"""
    await session.execute(
        update(AppLease)
        .where(
            AppLease.app_id == key,
            AppLease.holder_instance == instance_id,
            AppLease.generation == generation,
        )
        .values(
            holder_instance=None,
            released_at=func.now(),
            connection_state=state,
            drain_requested_by=None,
            drain_requested_at=None,
        )
    )


async def set_state(session: AsyncSession, key: uuid.UUID, state: str, *, instance_id: str) -> None:
    """只有当前持有者能写连接状态。"""
    await session.execute(
        update(AppLease)
        .where(AppLease.app_id == key, AppLease.holder_instance == instance_id)
        .values(connection_state=state)
    )


async def held_by(session: AsyncSession, instance_id: str) -> list[AppLease]:
    stmt = select(AppLease).where(AppLease.holder_instance == instance_id).order_by(AppLease.app_id)
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def disabled_among(session: AsyncSession, keys: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """这些应用里已经不该再连的：被停用、去掉了 assistant 能力或清空了 app_id。"""
    if not keys:
        return set()
    stmt = select(PlatformApp.id).where(PlatformApp.id.in_(list(keys)), ~_serviceable())
    return set((await session.execute(stmt)).scalars())
