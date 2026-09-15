"""task_streams 读写（spec §5.4、§6.3）。

worker 每次写都让 version+1；网关按 version > pushed_version 推送后回写 pushed_version。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.notify import notify
from coreman.core.db.models import TaskStream

WRITABLE = frozenset(
    {
        "thinking_md",
        "pending_text",
        "final_text",
        "pending_card",
        "session_url",
        "segment_boundaries",
        "background_state",
        "delivery_mode",
    }
)


async def create(
    session: AsyncSession,
    *,
    task_id: int,
    bot_id: uuid.UUID,
    platform: str,
    stream_id: str,
    reply_context: dict[str, Any],
    lease_generation: int,
    running_since: datetime,
    session_url: str | None = None,
    delivery_mode: str = "stream",
    background_state: dict[str, Any] | None = None,
) -> TaskStream:
    """建流。

    `background_state` 只在「从创建就是主动模式」时给（提交轮）；不给就一个字段都不赋，
    让列的 server_default `'{}'::jsonb` 生效——这一列非空，显式传 None 会插出约束违规。
    """
    row = TaskStream(
        task_id=task_id,
        bot_id=bot_id,
        platform=platform,
        stream_id=stream_id,
        reply_context=reply_context,
        lease_generation=lease_generation,
        running_since=running_since,
        session_url=session_url,
        delivery_mode=delivery_mode,
        **({"background_state": background_state} if background_state else {}),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update_state(session: AsyncSession, task_id: int, **fields: Any) -> Completion:
    """写入允许的增量字段并 version+1；返回原子更新后的投递状态，同事务 notify stream_updated。"""
    bad = set(fields) - WRITABLE
    if bad:
        raise ValueError(f"不可写字段: {sorted(bad)}")
    row = (
        await session.execute(
            sa_update(TaskStream)
            .where(TaskStream.task_id == task_id)
            .values(version=TaskStream.version + 1, updated_at=func.now(), **fields)
            .returning(
                TaskStream.version,
                TaskStream.bot_id,
                TaskStream.delivery_mode,
                TaskStream.finish_pushed_at,
                TaskStream.background_state,
            )
        )
    ).first()
    if row is None:
        raise LookupError(f"task_streams {task_id} 不存在")
    await notify(session, "stream_updated", {"task_id": task_id, "bot_id": str(row[1])})
    return Completion(int(row[0]), str(row[2]), row[3], row[4])


async def update(session: AsyncSession, task_id: int, **fields: Any) -> int:
    """兼容调用方只读取新版本号。"""
    return (await update_state(session, task_id, **fields)).version


def offset_of(state: dict[str, Any] | None) -> int:
    """`background_state.offset`：已经投递给用户的前缀长度，缺省 / 脏值一律当 0。

    worker 与网关都按它决定「还要推哪一段」，两边必须是同一套解析，否则一边多推一段、
    另一边就少推一段。
    """
    try:
        return max(int((state or {}).get("offset") or 0), 0)
    except (TypeError, ValueError):
        return 0


async def switch_to_proactive(
    session: AsyncSession, task_id: int, *, state: dict[str, Any]
) -> int | None:
    """把**未完成**的流翻成 proactive 并 version+1；已经完成的返回 None（不改行）。

    `is_complete = false` 就是网关与 worker 之间的那把锁：worker 的 `complete` 抢先落地时
    这条 UPDATE 不命中，网关据此知道「终稿已经写好、而且 worker 以为还是我在推」，必须自己
    把它送出去；命中了则相反，终稿归 worker 的 outbox。两条 UPDATE 由行锁串行，不会都以为
    对方负责。
    """
    row = (
        await session.execute(
            sa_update(TaskStream)
            .where(TaskStream.task_id == task_id, TaskStream.is_complete.is_(False))
            .values(
                delivery_mode="proactive",
                background_state=state,
                version=TaskStream.version + 1,
                updated_at=func.now(),
            )
            .returning(TaskStream.version, TaskStream.bot_id)
        )
    ).first()
    if row is None:
        return None
    await notify(session, "stream_updated", {"task_id": task_id, "bot_id": str(row[1])})
    return int(row[0])


@dataclass(frozen=True)
class Completion:
    """`complete` 的返回值：新版本号 + 这一行此刻的投递状态。

    投递状态必须由收尾的那条 UPDATE 自己带回来：worker 读一遍再判断的话，网关的
    drain / takeover 可以正好插在读与写之间，两边都以为对方会把终稿送出去。行锁把
    这条 UPDATE 与网关那条串行化，所以 RETURNING 回来的就是「谁后写谁说了算」的结论。

    Attributes:
        version: 收尾后的版本号
        delivery_mode: `stream`（还归网关推）/ `proactive`（终稿得走 outbox）
        finish_pushed_at: 网关已经推过 finish 的时刻
        background_state: `{mode, offset, finish_suffix, switched_at}`，`offset` 是已投递长度
    """

    version: int
    delivery_mode: str
    finish_pushed_at: datetime | None
    background_state: dict[str, Any] | None

    @property
    def offset(self) -> int:
        """已经投递给用户的前缀长度（没有记录就是 0）。"""
        return offset_of(self.background_state)


async def complete(
    session: AsyncSession,
    task_id: int,
    *,
    final_text: str,
    pending_card: dict[str, Any] | None = None,
) -> Completion:
    row = (
        await session.execute(
            sa_update(TaskStream)
            .where(TaskStream.task_id == task_id)
            .values(
                is_complete=True,
                completed_at=func.now(),
                final_text=final_text,
                pending_card=pending_card,
                version=TaskStream.version + 1,
                updated_at=func.now(),
            )
            .returning(
                TaskStream.version,
                TaskStream.bot_id,
                TaskStream.delivery_mode,
                TaskStream.finish_pushed_at,
                TaskStream.background_state,
            )
        )
    ).first()
    if row is None:
        raise LookupError(f"task_streams {task_id} 不存在")
    await notify(session, "stream_updated", {"task_id": task_id, "bot_id": str(row[1])})
    return Completion(int(row[0]), str(row[2]), row[3], row[4])


def _pending_clause() -> ColumnElement[bool]:
    return or_(
        TaskStream.version > TaskStream.pushed_version,
        TaskStream.is_complete.is_(True) & TaskStream.finish_pushed_at.is_(None),
    )


async def pending_for_bot(
    session: AsyncSession, bot_id: uuid.UUID, *, limit: int = 200
) -> list[TaskStream]:
    stmt = (
        select(TaskStream)
        .where(TaskStream.bot_id == bot_id, _pending_clause())
        .order_by(TaskStream.task_id)
        .limit(limit)
    )
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def bots_with_pending(session: AsyncSession, bot_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """兜底轮询用：这些 bot 里哪些有待推的流（判据与 `pending_for_bot` 同源）。

    一条语句代替「每个 bot 开一个事务跑一次 `pending_for_bot`」。
    """
    if not bot_ids:
        return set()
    stmt = (
        select(TaskStream.bot_id)
        .where(TaskStream.bot_id.in_(list(bot_ids)), _pending_clause())
        .distinct()
    )
    return set((await session.execute(stmt)).scalars())


async def active_for_bot(session: AsyncSession, bot_id: uuid.UUID) -> list[TaskStream]:
    stmt = (
        select(TaskStream)
        .where(
            TaskStream.bot_id == bot_id,
            or_(TaskStream.is_complete.is_(False), TaskStream.finish_pushed_at.is_(None)),
        )
        .order_by(TaskStream.task_id)
    )
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def mark_pushed(session: AsyncSession, task_id: int, version: int) -> None:
    """只前进不回退：并发推送下旧版本号不得覆盖新版本号。"""
    await session.execute(
        sa_update(TaskStream)
        .where(TaskStream.task_id == task_id)
        .values(pushed_version=func.greatest(TaskStream.pushed_version, version))
    )


async def mark_finish_pushed(session: AsyncSession, task_id: int) -> None:
    await session.execute(
        sa_update(TaskStream)
        .where(TaskStream.task_id == task_id)
        .values(finish_pushed_at=func.now())
    )


async def get(session: AsyncSession, task_id: int) -> TaskStream | None:
    return await session.get(TaskStream, task_id, populate_existing=True)
