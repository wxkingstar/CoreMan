"""批量写操作的视图：近期批次的列表、每一项的写入参数与结果摘要，以及停止一个在跑或暂停的批次。

谁能看：管理员（ai_committee、platform_admin）看全部；其他人看自己发起的，和自己能访问的
AI 员工（创建者、成员、团队负责人看本团队，同会话记录）的批次。谁能停：管理员和发起人本人。
每一项的请求与结果加密暂存，按页解密给能看的人；发起人以外的人查看明细记审计。批次在结束
24 小时后清理，列表也就只有近期的。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import ColumnElement, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.routers.chat_logs import accessible_bot_ids
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import Bot, InteractionState, SystemBatchItem, User
from coreman.core.systems_catalog import batch

router = APIRouter(
    prefix="/api/admin/system-batches",
    tags=["system-batches"],
    dependencies=[Depends(verify_csrf)],
)
MANAGER_ROLES = ("ai_committee", "platform_admin")
# 明细的状态筛选 → 库里的状态。
ITEM_FILTERS = {
    "ok": ("ok",),
    "failed": ("http_error", "unreachable"),
    "unknown": ("unknown", "sending"),
    "skipped": ("skipped",),
    "pending": ("pending",),
}


def _asker(state: InteractionState) -> str | None:
    value = state.state.get("user_id")
    return str(value) if value else None


async def _scope(session: AsyncSession, viewer: User) -> ColumnElement[bool] | None:
    """能看到的批次；None 表示不限。"""
    bots = await accessible_bot_ids(session, viewer)
    if bots is None:
        return None
    mine = InteractionState.state["user_id"].astext == str(viewer.id)
    return or_(mine, InteractionState.bot_id.in_(bots)) if bots else or_(mine, false())


async def _visible(session: AsyncSession, viewer: User, batch_id: uuid.UUID) -> InteractionState:
    state = await session.get(InteractionState, batch_id)
    if state is None or state.kind != batch.KIND:
        raise not_found("批次不存在")
    bots = await accessible_bot_ids(session, viewer)
    if bots is not None and _asker(state) != str(viewer.id) and state.bot_id not in bots:
        raise not_found("批次不存在")
    return state


def _can_stop(viewer: User, state: InteractionState) -> bool:
    return viewer.role in MANAGER_ROLES or _asker(state) == str(viewer.id)


async def _out(
    session: AsyncSession, rows: list[InteractionState], viewer: User
) -> list[dict[str, Any]]:
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
                "can_stop": _can_stop(viewer, row),
            }
        )
    return out


@router.get("")
async def list_batches(
    viewer: User = Depends(current_user),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(InteractionState)
        .where(InteractionState.kind == batch.KIND)
        .order_by(InteractionState.created_at.desc(), InteractionState.id)
    )
    scope = await _scope(session, viewer)
    if scope is not None:
        stmt = stmt.where(scope)
    page = await paginate(session, stmt, params)
    return {"code": 0, "data": {**page, "items": await _out(session, page["items"], viewer)}}


@router.get("/{batch_id}/items")
async def list_items(
    batch_id: uuid.UUID,
    request: Request,
    status: str | None = Query(None, pattern="^(ok|failed|unknown|skipped|pending)$"),
    viewer: User = Depends(current_user),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """一个批次按行号排的各项：写入参数（暂存时的原样）、状态与结果摘要，按页解密。"""
    state = await _visible(session, viewer, batch_id)
    stmt = (
        select(SystemBatchItem)
        .where(SystemBatchItem.batch_id == batch_id)
        .order_by(SystemBatchItem.seq)
    )
    if status is not None:
        stmt = stmt.where(SystemBatchItem.status.in_(ITEM_FILTERS[status]))
    page = await paginate(session, stmt, params)
    cipher = request.app.state.cipher
    items = []
    for item in page["items"]:
        aad = batch.item_aad(batch_id, item.seq)
        sent = json.loads(cipher.decrypt(item.request_enc, aad))
        items.append(
            {
                "seq": item.seq,
                "target": sent.get("target"),
                "path_params": sent.get("path_params"),
                "query": sent.get("query"),
                "body": sent.get("body"),
                "before": sent.get("before"),
                "status": item.status,
                "status_code": item.status_code,
                "brief": cipher.decrypt(item.brief_enc, aad) if item.brief_enc else None,
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            }
        )
    if _asker(state) != str(viewer.id):
        # 看的是别人提交的业务数据：记一笔，翻页也各记一笔。
        await record_audit(
            session,
            actor_id=viewer.id,
            actor_login=viewer.login_name,
            action="system_batch.view_items",
            target_type="system_batch",
            target_id=str(batch_id),
            diff={"page": params.page, "status": status},
            ip=client_ip(request),
        )
        await session.commit()
    return {"code": 0, "data": {**page, "items": items}}


@router.post("/{batch_id}/stop")
async def stop_batch(
    batch_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _visible(session, actor, batch_id)
    state = await session.get(
        InteractionState, batch_id, with_for_update=True, populate_existing=True
    )
    if state is None or state.kind != batch.KIND:
        raise not_found("批次不存在")
    if not _can_stop(actor, state):
        raise forbidden()
    by_asker = actor.role not in MANAGER_ROLES
    if not await batch.admin_stop(session, request.app.state.cipher, state, by_asker=by_asker):
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
    (item,) = await _out(session, [state], actor)
    return {"code": 0, "data": item}
