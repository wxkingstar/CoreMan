"""process_instances 读写（spec §5.4、§6.2 排空）。"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ProcessInstance


async def register(
    session: AsyncSession, *, instance_id: str, service: str, version: str, capacity: int | None
) -> ProcessInstance:
    """进程启动时登记；同 id 重启视为新一轮（清 stopped_at / drain_requested_at、running 归零）。"""
    stmt = insert(ProcessInstance).values(
        id=instance_id,
        service=service,
        version=version,
        capacity=capacity,
        started_at=func.now(),
        heartbeat_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProcessInstance.id],
        set_={
            "service": service,
            "version": version,
            "capacity": capacity,
            "started_at": func.now(),
            "heartbeat_at": func.now(),
            "stopped_at": None,
            "drain_requested_at": None,
            "running": 0,
        },
    )
    await session.execute(stmt)
    fresh = select(ProcessInstance).where(ProcessInstance.id == instance_id)
    result = await session.execute(fresh, execution_options={"populate_existing": True})
    return result.scalar_one()


async def heartbeat(session: AsyncSession, instance_id: str, running: int) -> bool:
    """写心跳与在跑任务数；返回本实例是否已被要求排空。

    心跳同时清掉 stopped_at：宿主机休眠或数据库抖动时 scheduler 会把心跳中断超过 60 秒的实例
    标记为已停止，进程恢复后若不自愈，就绪检查与排空请求会一直认为它已退出，7 天后还会删掉
    它仍在引用的实例行。正常退出先停心跳循环再写 stopped_at，不受影响。
    """
    row = (
        await session.execute(
            update(ProcessInstance)
            .where(ProcessInstance.id == instance_id)
            .values(heartbeat_at=func.now(), running=running, stopped_at=None)
            .returning(ProcessInstance.drain_requested_at)
        )
    ).first()
    return bool(row and row[0] is not None)


async def mark_stopped(session: AsyncSession, instance_id: str) -> None:
    await session.execute(
        update(ProcessInstance)
        .where(ProcessInstance.id == instance_id)
        .values(stopped_at=func.now())
    )


async def request_drain(session: AsyncSession, instance_id: str) -> bool:
    row = (
        await session.execute(
            update(ProcessInstance)
            .where(ProcessInstance.id == instance_id, ProcessInstance.stopped_at.is_(None))
            .values(drain_requested_at=func.now())
            .returning(ProcessInstance.id)
        )
    ).first()
    return row is not None


async def list_instances(session: AsyncSession) -> list[ProcessInstance]:
    stmt = select(ProcessInstance).order_by(ProcessInstance.service, ProcessInstance.id)
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
