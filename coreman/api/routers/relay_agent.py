"""Relay 管理台实时状态与手动探测。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.routers.relay_servers import load_relay
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.relay_policy import relay_visible
from coreman.core.db.models import Bot, ChatLog, User
from coreman.core.relay.agent_client import AgentError, call_agent

router = APIRouter(
    prefix="/api/admin/relay-servers", tags=["relay-agent"], dependencies=[Depends(verify_csrf)]
)
MANAGERS = require_roles("ai_committee", "platform_admin")


class ProbeIn(BaseModel):
    operation: Literal["probe-rate-limits", "health-check"]


@router.get("/{relay_id}/today-usage")
async def today_usage(
    relay_id: uuid.UUID, _: User = Depends(MANAGERS), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    await load_relay(session, relay_id)
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    columns = (
        ChatLog.input_tokens,
        ChatLog.output_tokens,
        ChatLog.cache_read_tokens,
        ChatLog.cache_creation_tokens,
        ChatLog.cost_usd,
    )
    row = (
        await session.execute(
            select(
                func.count(),
                *[func.sum(c) for c in columns],
                func.count(ChatLog.input_tokens),
                func.count(ChatLog.cost_usd),
            ).where(
                ChatLog.relay_server_id == relay_id,
                ChatLog.request_at >= start,
                ChatLog.request_at < start + timedelta(days=1),
            )
        )
    ).one()
    # 未上报 token/价格时保持 null，不能用 0 冒充测量结果。
    data = dict(
        zip(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "cost_usd",
            ),
            row[1:6],
            strict=True,
        )
    )
    return {
        "code": 0,
        "data": {
            "timezone": "UTC",
            "since": start.isoformat(),
            "total": row[0],
            **data,
            "token_reported": row[6],
            "cost_reported": row[7],
        },
    }


@router.get("/{relay_id}/live-status")
async def live_status(
    relay_id: uuid.UUID,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    if not relay_visible(user, relay):
        raise not_found("实例不存在")
    try:
        data = await call_agent(relay, request.app.state.cipher, "status")
    except AgentError as exc:
        raise ApiError(502, 502, str(exc)) from exc
    tasks = data.get("active_tasks", [])
    keys = {task.get("bot_key") for task in tasks if task.get("bot_key")}
    names: dict[str, str] = {}
    if keys:
        rows = await session.execute(select(Bot.bot_key, Bot.name).where(Bot.bot_key.in_(keys)))
        names = {row[0]: row[1] for row in rows.all()}
    for task in tasks:
        task["bot_name"] = names.get(task.get("bot_key"))
    return {"code": 0, "data": data}


@router.post("/{relay_id}/agent-probe")
async def agent_probe(
    relay_id: uuid.UUID,
    body: ProbeIn,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    relay = await load_relay(session, relay_id)
    try:
        data = await call_agent(relay, request.app.state.cipher, body.operation)
    except AgentError as exc:
        raise ApiError(502, 502, str(exc)) from exc
    await record_audit(
        session,
        action="relay.agent_probe",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="relay",
        target_id=str(relay_id),
        diff={"operation": [None, body.operation]},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": data}
