"""运行时席位：一个节点上同一种 AI 登录多个账号，由平台管理员在管理台添加、登录、切换、移除。

席位与登录状态以节点为准（节点在心跳的 capabilities.<AI>.seats 里上报，操作结果也会带回最新
清单）；登录由节点上的 CLI 完成，令牌留在节点，平台只转交授权链接与浏览器页面上的认证码。
切换时把旧席位的额度存进 relay_servers.seat_quotas，再换上新席位上次的额度。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import RelayServer, RuntimeNode, User
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.runtime_nodes.transport import online

router = APIRouter(prefix="/api/admin/runtime-nodes", dependencies=[Depends(verify_csrf)])
ADMINS = require_roles("platform_admin")
Provider = Literal["claude", "codex"]
SEAT_ID = r"^(default|[a-f0-9]{12})$"
QUOTA_COLUMNS = (
    "rate_limit_5h_used_pct",
    "rate_limit_5h_resets_at",
    "rate_limit_7d_used_pct",
    "rate_limit_7d_resets_at",
    "rate_limit_probed_at",
)


class SeatIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)


class LoginCompleteIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    # Claude 浏览器页面给出的认证码；Codex 设备码登录不需要。
    code: str | None = Field(default=None, max_length=2000)


async def target(
    session: AsyncSession, node_id: uuid.UUID, provider: str
) -> tuple[RuntimeNode, RelayServer]:
    node = await session.get(RuntimeNode, node_id)
    if node is None:
        raise not_found("运行时不存在")
    relay = await session.scalar(
        select(RelayServer).where(
            RelayServer.runtime_node_id == node_id, RelayServer.model_provider == provider
        )
    )
    if relay is None:
        raise not_found("运行时实例不存在")
    if not node.is_active or not online(node):
        raise ApiError(409, 409, "运行时不在线，请等它上线后再操作")
    cap = (node.capabilities or {}).get(provider) or {}
    if cap.get("installed") is not True:
        raise ApiError(409, 409, "这台运行时没有安装该 AI")
    if cap.get("seat_switch_v1") is not True:
        raise ApiError(409, 409, "请先升级 Runtime，再管理席位")
    # 下面要等节点（登录还要等管理员授权），不占着数据库事务。
    await session.commit()
    return node, relay


async def seat_call(
    relay: RelayServer, request: Request, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await call_agent(relay, request.app.state.cipher, operation, payload)
    except AgentError as exc:
        if exc.detail:
            raise ApiError(422, 422, exc.detail) from exc
        raise ApiError(502, 502, "席位操作未完成，请检查运行时在线状态") from exc


async def store_seats(
    session: AsyncSession, node_id: uuid.UUID, provider: str, seats: Any
) -> list[dict[str, Any]]:
    """节点在操作结果里带回的最新席位清单，先写进 capabilities，管理台不必等下一次心跳。"""
    from coreman.api.routers.runtime_protocol import SeatState

    states = [SeatState.model_validate(s).model_dump() for s in (seats or [])][:21]
    node = await session.get(RuntimeNode, node_id, with_for_update=True)
    if node is not None:
        caps = dict(node.capabilities or {})
        caps[provider] = {**(caps.get(provider) or {}), "seats": states}
        node.capabilities = caps
    return states


async def audit(
    session: AsyncSession,
    request: Request,
    actor: User,
    action: str,
    node_id: uuid.UUID,
    diff: dict[str, Any],
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="runtime_node",
        target_id=str(node_id),
        diff=diff,
        ip=client_ip(request),
    )


def active_seat(node: RuntimeNode, provider: str) -> str:
    seats = ((node.capabilities or {}).get(provider) or {}).get("seats") or []
    return next((s["id"] for s in seats if s.get("active")), "default")


def dump_quota(relay: RelayServer) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in QUOTA_COLUMNS:
        value = getattr(relay, column)
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        out[column] = value
    return out


def load_quota(relay: RelayServer, saved: dict[str, Any]) -> None:
    for column in QUOTA_COLUMNS:
        value = saved.get(column)
        if value is not None and column.endswith("_pct"):
            value = Decimal(str(value))
        elif isinstance(value, str):
            value = datetime.fromisoformat(value)
        setattr(relay, column, value)


@router.post("/{node_id}/{provider}/seats", status_code=201)
async def create_seat(
    node_id: uuid.UUID,
    provider: Provider,
    body: SeatIn,
    request: Request,
    actor: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _, relay = await target(session, node_id, provider)
    result = await seat_call(relay, request, "seat-create", {"label": body.label})
    seats = await store_seats(session, node_id, provider, result.get("seats"))
    await audit(
        session,
        request,
        actor,
        "runtime.seat_create",
        node_id,
        {
            "provider": [None, provider],
            "seat": [None, result.get("seat_id")],
            "label": [None, body.label],
        },
    )
    await session.commit()
    return {"code": 0, "data": {"seat_id": result.get("seat_id"), "seats": seats}}


@router.post("/{node_id}/{provider}/seats/{seat_id}/login")
async def start_login(
    node_id: uuid.UUID,
    provider: Provider,
    seat_id: str,
    request: Request,
    actor: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """节点上的 CLI 发起登录，返回授权链接（Codex 另给一次性代码）。"""
    del actor
    _, relay = await target(session, node_id, provider)
    data = await seat_call(relay, request, "seat-login-start", {"seat_id": seat_path(seat_id)})
    keys = ("session_id", "authorization_url", "user_code", "needs_code", "expires_in")
    return {"code": 0, "data": {key: data.get(key) for key in keys}}


@router.post("/{node_id}/{provider}/seats/{seat_id}/login/complete")
async def complete_login(
    node_id: uuid.UUID,
    provider: Provider,
    seat_id: str,
    body: LoginCompleteIn,
    request: Request,
    actor: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _, relay = await target(session, node_id, provider)
    result = await seat_call(
        relay,
        request,
        "seat-login-complete",
        {"seat_id": seat_path(seat_id), "session_id": body.session_id, "code": body.code},
    )
    seats = await store_seats(session, node_id, provider, result.get("seats"))
    await audit(session, request, actor, "runtime.seat_login", node_id, {"seat": [None, seat_id]})
    await session.commit()
    return {"code": 0, "data": {"seats": seats}}


@router.post("/{node_id}/{provider}/seats/{seat_id}/activate")
async def activate_seat(
    node_id: uuid.UUID,
    provider: Provider,
    seat_id: str,
    request: Request,
    actor: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """切换当前席位：新对话用新席位，节点上进行中的对话在原席位上跑完。"""
    node, relay = await target(session, node_id, provider)
    previous = active_seat(node, provider)
    result = await seat_call(relay, request, "seat-activate", {"seat_id": seat_path(seat_id)})
    seats = await store_seats(session, node_id, provider, result.get("seats"))
    if previous != seat_id:
        row = await session.get(RelayServer, relay.id, with_for_update=True)
        if row is not None:
            saved = dict(row.seat_quotas or {})
            saved[previous] = dump_quota(row)
            load_quota(row, saved.pop(seat_id, None) or {})
            row.seat_quotas = saved
        await audit(
            session, request, actor, "runtime.seat_activate", node_id, {"seat": [previous, seat_id]}
        )
    await session.commit()
    return {"code": 0, "data": {"seats": seats}}


@router.delete("/{node_id}/{provider}/seats/{seat_id}")
async def remove_seat(
    node_id: uuid.UUID,
    provider: Provider,
    seat_id: str,
    request: Request,
    actor: User = Depends(ADMINS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _, relay = await target(session, node_id, provider)
    result = await seat_call(relay, request, "seat-remove", {"seat_id": seat_path(seat_id)})
    seats = await store_seats(session, node_id, provider, result.get("seats"))
    row = await session.get(RelayServer, relay.id, with_for_update=True)
    if row is not None and seat_id in (row.seat_quotas or {}):
        row.seat_quotas = {k: v for k, v in (row.seat_quotas or {}).items() if k != seat_id}
    await audit(session, request, actor, "runtime.seat_remove", node_id, {"seat": [seat_id, None]})
    await session.commit()
    return {"code": 0, "data": {"seats": seats}}


def seat_path(seat_id: str) -> str:
    if not re.fullmatch(SEAT_ID, seat_id):
        raise ApiError(422, 422, "席位标识无效")
    return seat_id
