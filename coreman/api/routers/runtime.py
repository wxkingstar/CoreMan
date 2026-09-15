"""运行状态（总线表与排空）：实例、租约、队列、任务与出站箱。

读（team_lead 及以上）只看总线表，不碰进程；写（platform_admin）只写「请求」列——
重试、取消、排空都是把意图落库 + pg_notify，真正动手的是网关/工作进程自己。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bus import instances, leases, outbox, tasks
from coreman.core.db.models import (
    Bot,
    BotLease,
    OutboxItem,
    ProcessInstance,
    Task,
    TaskStream,
    User,
)

router = APIRouter(
    prefix="/api/admin/runtime", tags=["runtime"], dependencies=[Depends(verify_csrf)]
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_READERS = require_roles("team_lead", "ai_committee", "platform_admin")
_ADMINS = require_roles("platform_admin")
# 活体判定与 reaper 对齐：stopped_at 只由 reaper / 优雅退出写，心跳断了但没人收尸时靠这个。
ALIVE_SECONDS = 60
DEFAULT_LIMIT = 200


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class DrainIn(BaseModel):
    instance_id: str | None = Field(default=None, max_length=200)
    bot_key: str | None = Field(default=None, max_length=50)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def instance_out(row: ProcessInstance, now: datetime) -> dict[str, Any]:
    return {
        "id": row.id,
        "service": row.service,
        "version": row.version,
        "started_at": _iso(row.started_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "alive": row.heartbeat_at >= now - timedelta(seconds=ALIVE_SECONDS),
        "capacity": row.capacity,
        "running": row.running,
        "drain_requested_at": _iso(row.drain_requested_at),
        "stopped_at": _iso(row.stopped_at),
    }


def lease_out(row: BotLease, bot_key: str, bot_name: str) -> dict[str, Any]:
    return {
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "bot_key": bot_key,
        "bot_name": bot_name,
        "platform": row.platform,
        "holder_instance": row.holder_instance,
        "generation": row.generation,
        "connection_state": row.connection_state,
        "acquired_at": _iso(row.acquired_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "drain_requested_by": row.drain_requested_by,
        "released_at": _iso(row.released_at),
    }


def task_out(row: Task, bot_keys: dict[uuid.UUID, tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "bot_key": (
            bot_keys[row.bot_id][0] if row.bot_id is not None and row.bot_id in bot_keys else None
        ),
        "bot_name": (
            bot_keys[row.bot_id][1] if row.bot_id is not None and row.bot_id in bot_keys else None
        ),
        "kind": row.kind,
        "lane": row.lane,
        "priority": row.priority,
        "session_key": row.session_key,
        "status": row.status,
        "claimed_by": row.claimed_by,
        "run_after": _iso(row.run_after),
        "claimed_at": _iso(row.claimed_at),
        "started_at": _iso(row.started_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "cancel_requested_at": _iso(row.cancel_requested_at),
        "cancel_reason": row.cancel_reason,
        "attempts": row.attempts,
    }


def outbox_out(row: OutboxItem, bot_keys: dict[uuid.UUID, tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "bot_key": (
            bot_keys[row.bot_id][0] if row.bot_id is not None and row.bot_id in bot_keys else None
        ),
        "bot_name": (
            bot_keys[row.bot_id][1] if row.bot_id is not None and row.bot_id in bot_keys else None
        ),
        "kind": row.kind,
        "dedupe_key": row.dedupe_key,
        "status": row.status,
        "attempts": row.attempts,
        "not_before": _iso(row.not_before),
        "last_error": row.last_error,
        "created_at": _iso(row.created_at),
        "sent_at": _iso(row.sent_at),
    }


async def _bot_keys(
    session: AsyncSession, bot_ids: list[uuid.UUID | None]
) -> dict[uuid.UUID, tuple[str, str]]:
    """批量取 bot_key，避免列表里逐行查库。"""
    if not bot_ids:
        return {}
    rows = (
        await session.execute(select(Bot.id, Bot.bot_key, Bot.name).where(Bot.id.in_(bot_ids)))
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


@router.get("/instances")
async def list_instances(
    _: User = Depends(_READERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    now = datetime.now(UTC)
    rows = await instances.list_instances(session)
    # 已停的排在最后：管理台先看活着的。
    rows.sort(key=lambda r: (r.stopped_at is not None, r.service, r.id))
    return {"code": 0, "data": [instance_out(r, now) for r in rows]}


@router.get("/leases")
async def list_leases(
    _: User = Depends(_READERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    stmt = (
        select(BotLease, Bot.bot_key, Bot.name)
        .join(Bot, Bot.id == BotLease.bot_id)
        .order_by(BotLease.platform, Bot.bot_key)
    )
    rows = (await session.execute(stmt, execution_options={"populate_existing": True})).all()
    return {"code": 0, "data": [lease_out(r[0], r[1], r[2]) for r in rows]}


@router.get("/queue")
async def queue_overview(
    _: User = Depends(_READERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    depth = await tasks.queue_depth(session)
    pending = await outbox.count_pending(session)
    active_rows = (
        await session.execute(
            select(Task.status, func.count())
            .where(Task.status.in_(tasks.ACTIVE))
            .group_by(Task.status)
        )
    ).all()
    active = {str(row[0]): int(row[1]) for row in active_rows}
    failed = (
        await session.execute(
            select(func.count()).select_from(OutboxItem).where(OutboxItem.status == "failed")
        )
    ).scalar_one()
    # 与 task_streams_active_idx 的谓词一致：没写完，或者写完了还没把收尾推给平台。
    streams_active = (
        await session.execute(
            select(func.count())
            .select_from(TaskStream)
            .where(or_(TaskStream.is_complete.is_(False), TaskStream.finish_pushed_at.is_(None)))
        )
    ).scalar_one()
    return {
        "code": 0,
        "data": {
            "queued": {"normal": depth.get("normal", 0), "fast": depth.get("fast", 0)},
            "claimed": int(active.get("claimed", 0)),
            "running": int(active.get("running", 0)),
            "outbox_pending": pending,
            "outbox_failed": int(failed),
            "streams_active": int(streams_active),
        },
    }


@router.get("/tasks")
async def list_tasks(
    status: Literal["active", "queued"] = "active",
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=500),
    _: User = Depends(_READERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if status == "active":
        rows = await tasks.list_active(session, limit=limit)
    else:
        # 排队中按 worker 的抢单顺序列（tasks_claim_idx 的列序），看到的就是下一个跑谁。
        stmt = (
            select(Task)
            .where(Task.status == "queued")
            .order_by(Task.lane, Task.priority.desc(), Task.id)
            .limit(limit)
        )
        rows = list(
            (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
        )
    keys = await _bot_keys(session, [r.bot_id for r in rows])
    return {"code": 0, "data": [task_out(r, keys) for r in rows]}


@router.get("/outbox")
async def list_outbox(
    status: Literal["failed", "pending"] = "failed",
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=500),
    _: User = Depends(_READERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if status == "failed":
        rows = await outbox.list_failed(session, limit=limit)
    else:
        stmt = (
            select(OutboxItem)
            .where(OutboxItem.status == "pending")
            .order_by(OutboxItem.not_before, OutboxItem.id)
            .limit(limit)
        )
        rows = list(
            (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
        )
    keys = await _bot_keys(session, [r.bot_id for r in rows])
    return {"code": 0, "data": [outbox_out(r, keys) for r in rows]}


@router.post("/outbox/{item_id}/retry")
async def retry_outbox(
    item_id: int,
    request: Request,
    actor: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await session.get(OutboxItem, item_id, populate_existing=True)
    if item is None:
        raise not_found("出站项不存在")
    if item.status != "failed":
        raise ApiError(409, 409, "只有失败的出站项可以重试")
    await outbox.retry(session, item_id)
    await record_audit(
        session,
        action="runtime.outbox_retry",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="outbox",
        target_id=str(item_id),
        diff={"outbox_id": item_id, "bot_id": str(item.bot_id)},
        ip=client_ip(request),
    )
    await session.commit()
    # retry 是 Core UPDATE：内存里的 item 还是 failed，回给前端的必须是重投后的行。
    fresh = await session.get(OutboxItem, item_id, populate_existing=True)
    keys = await _bot_keys(session, [item.bot_id])
    return {"code": 0, "data": outbox_out(fresh, keys) if fresh else None}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    request: Request,
    body: CancelIn | None = None,
    actor: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await tasks.get(session, task_id)
    if row is None:
        raise not_found("任务不存在")
    if row.status not in tasks.OPEN:
        raise ApiError(409, 409, "任务已结束，无法取消")
    reason = (body.reason if body and body.reason else None) or "admin"
    # 已经请求过取消的再点一次是幂等的：request_cancel 返回 False，状态不变。
    await tasks.request_cancel(session, task_id, reason)
    await record_audit(
        session,
        action="runtime.task_cancel",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="task",
        target_id=str(task_id),
        diff={"task_id": task_id, "reason": reason},
        ip=client_ip(request),
    )
    await session.commit()
    fresh = await tasks.get(session, task_id)
    keys = await _bot_keys(session, [fresh.bot_id] if fresh else [])
    return {"code": 0, "data": task_out(fresh, keys) if fresh else None}


@router.post("/drain")
async def request_drain(
    body: DrainIn,
    request: Request,
    actor: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """排空：按实例（整个进程不再接活）或按 bot（只让这一个 bot 换个网关）二选一。"""
    if bool(body.instance_id) == bool(body.bot_key):
        raise ApiError(422, 422, "instance_id 与 bot_key 必须二选一")
    by = actor.login_name or "admin"
    if body.instance_id:
        if not await instances.request_drain(session, body.instance_id):
            raise not_found("实例不存在或已停止")
        target_type, target_id = "process_instance", body.instance_id
    else:
        bot = (
            await session.execute(select(Bot).where(Bot.bot_key == body.bot_key))
        ).scalar_one_or_none()
        if bot is None:
            raise not_found("机器人不存在")
        if await leases.live_holder(session, bot.id) is None:
            # 没有在线的网关持有它，这个标记就没人会执行：行上挂着「排空中」，按钮还按不动。
            raise ApiError(409, 409, "该机器人当前没有在线的网关持有，无法排空")
        await leases.request_drain(session, bot.id, by=by)
        target_type, target_id = "bot", str(bot.id)
    await record_audit(
        session,
        action="runtime.drain",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type=target_type,
        target_id=target_id,
        diff={"instance_id": body.instance_id, "bot_key": body.bot_key, "by": by},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": {"target_type": target_type, "target_id": target_id}}
