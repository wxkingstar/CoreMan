"""运行时实例（relay_servers）：列表、健康探测与目录预填、有效模型集。

实例由运行时节点注册时自动创建（每个节点按 AI 类型各一行），管理台不再手工增删改。
读：任何登录用户，但 `visibility='admins'` 的实例只对 ai_committee / platform_admin 可见；
探测：ai_committee / platform_admin。
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.routers.model_catalog import load_catalog
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.relay_policy import backend_unavailable_reason, relay_visible
from coreman.core.db.models import Bot, ModelCatalog, RelayServer, RuntimeNode, Team, User
from coreman.core.relay import probe
from coreman.core.relay.client import RelayClient
from coreman.core.relay.models import default_model, effective_models

router = APIRouter(
    prefix="/api/admin/relay-servers", tags=["relay-servers"], dependencies=[Depends(verify_csrf)]
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_MANAGERS = require_roles("ai_committee", "platform_admin")
MANAGER_ROLES = ("ai_committee", "platform_admin")


async def load_relay(session: AsyncSession, relay_id: uuid.UUID) -> RelayServer:
    """按 id 取 relay，取不到就 404（relay_agent 路由复用）。"""
    relay = await session.get(RelayServer, relay_id)
    if relay is None:
        raise not_found("运行时不存在")
    return relay


async def relay_out(
    session: AsyncSession,
    relay: RelayServer,
    catalog: Sequence[ModelCatalog],
    team_names: dict[uuid.UUID, str],
    bot_counts: dict[uuid.UUID, int],
) -> dict[str, Any]:
    """对外视图。目录、团队名、bot 计数由调用方批量查好传进来，避免列表里逐行查库。"""
    eff = effective_models(relay, catalog)
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    return {
        "id": str(relay.id),
        "name": f"{node.name} / {relay.model_provider}" if node else relay.name,
        "runtime_node_id": str(node.id) if node else None,
        "runtime_name": node.name if node else None,
        "workspace_root": node.workspace_root if node else None,
        "unavailable_reason": backend_unavailable_reason(relay, node),
        "relay_url": relay.relay_url,
        "model_provider": relay.model_provider,
        "supported_models_mode": relay.supported_models_mode,
        "supported_models": relay.supported_models,
        "effective_models": eff,
        "default_model": default_model(relay.model_provider, catalog),
        "team_id": str(relay.team_id) if relay.team_id else None,
        "team_name": team_names.get(relay.team_id) if relay.team_id else None,
        "visibility": relay.visibility,
        "description": relay.description,
        "is_active": relay.is_active,
        "rate_limit_5h_used_pct": _pct(relay.rate_limit_5h_used_pct),
        "rate_limit_5h_resets_at": relay.rate_limit_5h_resets_at,
        "rate_limit_7d_used_pct": _pct(relay.rate_limit_7d_used_pct),
        "rate_limit_7d_resets_at": relay.rate_limit_7d_resets_at,
        "rate_limit_probed_at": relay.rate_limit_probed_at,
        "health_status": relay.health_status,
        "health_checked_at": relay.health_checked_at,
        "health_detail": relay.health_detail,
        "health_latency_ms": relay.health_latency_ms,
        "health_fail_count": relay.health_fail_count,
        "relay_version": relay.relay_version,
        "relay_mode": relay.relay_mode,
        "bot_count": bot_counts.get(relay.id, 0),
        "version": relay.version,
        "created_at": relay.created_at,
        "updated_at": relay.updated_at,
    }


def _pct(value: Any) -> float | None:
    """Numeric 列到 JSON：Decimal 前端读不了，统一转 float。"""
    return float(value) if value is not None else None


async def team_names(session: AsyncSession, team_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not team_ids:
        return {}
    rows = (await session.execute(select(Team).where(Team.id.in_(team_ids)))).scalars().all()
    return {t.id: t.name_zh for t in rows}


async def bot_counts(session: AsyncSession, relay_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not relay_ids:
        return {}
    rows = await session.execute(
        select(Bot.relay_server_id, func.count())
        .where(Bot.relay_server_id.in_(relay_ids))
        .group_by(Bot.relay_server_id)
    )
    return {rid: count for rid, count in rows.all() if rid is not None}


async def _one_out(session: AsyncSession, relay: RelayServer) -> dict[str, Any]:
    """单条 relay 的视图：目录只取它那个 provider。"""
    team_ids: set[uuid.UUID] = {relay.team_id} if relay.team_id else set()
    return await relay_out(
        session,
        relay,
        await load_catalog(session, relay.model_provider),
        await team_names(session, team_ids),
        await bot_counts(session, {relay.id}),
    )


async def _visible_relay(session: AsyncSession, relay_id: uuid.UUID, user: User) -> RelayServer:
    """读接口的取行：不可见的实例按「不存在」处理，不泄漏它的存在性。"""
    relay = await load_relay(session, relay_id)
    if not relay_visible(user, relay):
        raise not_found("运行时不存在")
    return relay


def _make_client(relay: RelayServer) -> RelayClient:
    """建客户端：实例未绑定运行时节点时按 422 回，不要变成 500。"""
    try:
        return probe.make_client(relay)
    except Exception as exc:
        raise ApiError(422, 422, "运行时未绑定节点，无法探测") from exc


@router.get("")
async def list_relays(
    user: User = Depends(current_user),
    team_id: uuid.UUID | None = None,
    model_provider: str | None = None,
    is_active: bool | None = None,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(RelayServer)
        .where(RelayServer.runtime_node_id.is_not(None))
        .order_by(RelayServer.name)
    )
    if user.role not in MANAGER_ROLES:
        stmt = stmt.where(RelayServer.visibility == "all")
    if team_id is not None:
        stmt = stmt.where(RelayServer.team_id == team_id)
    if model_provider:
        stmt = stmt.where(RelayServer.model_provider == model_provider)
    if is_active is not None:
        stmt = stmt.where(RelayServer.is_active == is_active)
    page = await paginate(session, stmt, params)
    relays: list[RelayServer] = page["items"]
    catalog = await load_catalog(session)
    names = await team_names(session, {r.team_id for r in relays if r.team_id})
    counts = await bot_counts(session, {r.id for r in relays})
    items = [await relay_out(session, r, catalog, names, counts) for r in relays]
    return {"code": 0, "data": {**page, "items": items}}


@router.post("/{relay_id}/probe")
async def probe_endpoint(
    relay_id: uuid.UUID,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    before = relay.health_status
    client: RelayClient | None = None
    try:
        client = _make_client(relay)
        health, added = await probe.probe_relay(session, relay, client)
    finally:
        if client is not None:
            await client.aclose()
    await record_audit(
        session,
        action="relay.probe",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="relay_server",
        target_id=str(relay.id),
        diff={"health_status": [before, relay.health_status]},
        ip=client_ip(request),
    )
    await session.commit()
    return {
        "code": 0,
        "data": {
            "health": dataclasses.asdict(health),
            "added_models": added,
            "relay": await _one_out(session, relay),
        },
    }


@router.get("/{relay_id}/models")
async def relay_models(
    relay_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await _visible_relay(session, relay_id, user)
    catalog = await load_catalog(session, relay.model_provider)
    return {
        "code": 0,
        "data": {
            "provider": relay.model_provider,
            "mode": relay.supported_models_mode,
            "models": effective_models(relay, catalog),
            "default": default_model(relay.model_provider, catalog),
        },
    }
