"""批量写操作的管理台视图：近期批次的列表，以及由管理员停止一个在跑或暂停的批次。

只出元数据（谁、哪个员工、哪个系统的哪个操作、多少项、各状态几项）；每一项的请求与结果是
加密暂存的业务数据，这里不解密、不提供下载。批次在结束 24 小时后清理，列表也就只有近期的。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import Bot, InteractionState, SystemBatchItem, User
from coreman.core.systems_catalog import batch

router = APIRouter(
    prefix="/api/admin/system-batches",
    tags=["system-batches"],
    dependencies=[Depends(verify_csrf)],
)
_MANAGERS = require_roles("ai_committee", "platform_admin")


async def _out(session: AsyncSession, rows: list[InteractionState]) -> list[dict[str, Any]]:
    ids = [row.id for row in rows]
    counts: dict[uuid.UUID, dict[str, int]] = {identity: {} for identity in ids}
    if ids:
        found = await session.execute(
            select(SystemBatchItem.batch_id, SystemBatchItem.status, func.count())
            .where(SystemBatchItem.batch_id.in_(ids))
            .group_by(SystemBatchItem.batch_id, SystemBatchItem.status)
        )
        for batch_id, status, n in found.all():
            counts[batch_id][status] = int(n)
    bot_ids = {row.bot_id for row in rows}
    user_ids = {uuid.UUID(str(row.state["user_id"])) for row in rows if row.state.get("user_id")}
    bots: dict[uuid.UUID, str] = {}
    users: dict[uuid.UUID, str] = {}
    if bot_ids:
        found_bots = await session.execute(select(Bot.id, Bot.name).where(Bot.id.in_(bot_ids)))
        bots = {identity: name for identity, name in found_bots.all()}
    if user_ids:
        found_users = await session.execute(
            select(User.id, User.display_name).where(User.id.in_(user_ids))
        )
        users = {identity: name for identity, name in found_users.all()}
    out = []
    for row in rows:
        meta, tally = row.state, counts[row.id]
        failed = tally.get("http_error", 0) + tally.get("unreachable", 0)
        out.append(
            {
                "id": str(row.id),
                "phase": batch.phase(row),
                "pause_reason": (meta.get("paused") or {}).get("reason"),
                "stopping": bool(meta.get(batch.STOP)) and not meta.get("finished_at"),
                "bot_id": str(row.bot_id),
                "bot_name": bots.get(row.bot_id),
                "user_id": meta.get("user_id"),
                "user_name": users.get(uuid.UUID(str(meta["user_id"])))
                if meta.get("user_id")
                else None,
                "system": meta.get("system"),
                "system_name": meta.get("system_name"),
                "operation_id": meta.get("operation_id"),
                "operation_summary": meta.get("summary") or "",
                "risk": meta.get("risk"),
                "count": int(meta.get("count") or 0),
                "ok": tally.get("ok", 0),
                "failed": failed,
                "unknown": tally.get("unknown", 0) + tally.get("sending", 0),
                "skipped": tally.get("skipped", 0),
                "pending": tally.get("pending", 0),
                "created_at": row.created_at.isoformat(),
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
            }
        )
    return out


@router.get("")
async def list_batches(
    _: User = Depends(_MANAGERS),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(InteractionState)
        .where(InteractionState.kind == batch.KIND)
        .order_by(InteractionState.created_at.desc(), InteractionState.id)
    )
    page = await paginate(session, stmt, params)
    return {"code": 0, "data": {**page, "items": await _out(session, page["items"])}}


@router.post("/{batch_id}/stop")
async def stop_batch(
    batch_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    state = await session.get(InteractionState, batch_id, with_for_update=True)
    if state is None or state.kind != batch.KIND:
        raise not_found("批次不存在")
    if not await batch.admin_stop(session, request.app.state.cipher, state):
        raise ApiError(409, 409, "这个批次不在执行中，或已经在停止")
    await record_audit(
        session,
        actor_id=actor.id,
        actor_login=actor.login_name,
        action="system_batch.stop",
        target_type="system_batch",
        target_id=str(batch_id),
        diff={"system": state.state.get("system"), "operation_id": state.state.get("operation_id")},
        ip=client_ip(request),
    )
    await session.commit()
    (item,) = await _out(session, [state])
    return {"code": 0, "data": item}
