"""Relay 遥测。agent token 只能操作所属实例，签名调用方必须有 relay scope。"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.infra_auth import require_scope, signed_client
from coreman.core.crypto import DecryptError
from coreman.core.db.models import RelayServer
from coreman.core.relay.models import effective_models, load_catalog

router = APIRouter(tags=["infra-relay"])
RELAY_SCOPE = require_scope("relay")


class ReportTarget(BaseModel):
    server_id: uuid.UUID | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    name: str | None = Field(default=None, max_length=100)
    model_provider: str | None = Field(default=None, max_length=100)


class QuotaWindow(BaseModel):
    used_percentage: float | None = Field(default=None, ge=0, le=100)
    resets_at: datetime | None = None

    @field_validator("resets_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class Quota(BaseModel):
    five_hour: QuotaWindow | None = None
    seven_day: QuotaWindow | None = None


class QuotaReport(ReportTarget):
    rate_limits: Quota


class HealthReport(ReportTarget):
    status: Literal["healthy", "down", "auth_fail", "timeout", "unknown"]
    detail: str | None = Field(default=None, max_length=255)
    latency_ms: int | None = Field(default=None, ge=0, le=3600000)


async def target(
    request: Request, body: ReportTarget, session: AsyncSession, *, scope: str = "relay"
) -> RelayServer:
    authorization = request.headers.get("Authorization", "")
    bearer = authorization.startswith("Bearer ")
    if not bearer:
        client = await signed_client(request, session)
        await require_scope(scope)(client, session)
    if body.server_id is None and body.port is None:
        raise ApiError(422, 422, "需要 server_id 或 agent 端口")
    stmt = select(RelayServer).where(RelayServer.is_active)
    if body.server_id is not None:
        stmt = stmt.where(RelayServer.id == body.server_id)
    if body.port is not None:
        stmt = stmt.where(RelayServer.agent_port == body.port)
    if body.name:
        stmt = stmt.where(RelayServer.name == body.name)
    if body.model_provider:
        stmt = stmt.where(RelayServer.model_provider == body.model_provider)
    rows = list((await session.execute(stmt)).scalars())
    if bearer:
        raw = authorization.removeprefix("Bearer ")
        valid = []
        if 16 <= len(raw) <= 512:
            for row in rows:
                if not row.agent_token_enc:
                    continue
                try:
                    token = request.app.state.cipher.decrypt(
                        row.agent_token_enc, "relay_servers.agent_token_enc"
                    )
                except DecryptError:
                    continue
                if secrets.compare_digest(raw.encode(), token.encode()):
                    valid.append(row)
        if len(valid) != 1:
            raise ApiError(401, 401, "实例令牌无效或目标不匹配")
        return valid[0]
    if not rows:
        raise ApiError(404, 404, "未找到对应的实例")
    if len(rows) != 1:
        raise ApiError(409, 409, "目标不唯一，请补充 server_id 或名称")
    return rows[0]


@router.post("/api/infra/relay/rate-limits")
@router.post("/api/robot/rate-limits/report", include_in_schema=False)
async def rate_limits(
    body: QuotaReport, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    relay = await target(request, body, session)
    values: dict[str, Any] = {"rate_limit_probed_at": datetime.now(UTC)}
    windows = (body.rate_limits.five_hour, body.rate_limits.seven_day)
    # 空额度只是心跳，保留上次采集值（旧 git_webhook 在额度耗尽时会发 {}）。
    if any(windows):
        for label, window in zip(("5h", "7d"), windows, strict=True):
            values[f"rate_limit_{label}_used_pct"] = window.used_percentage if window else None
            values[f"rate_limit_{label}_resets_at"] = window.resets_at if window else None
    await session.execute(update(RelayServer).where(RelayServer.id == relay.id).values(**values))
    await session.commit()
    return {"success": True, "code": 0, "data": None}


@router.post("/api/infra/relay/health")
@router.post("/api/robot/health/report", include_in_schema=False)
async def health(
    body: HealthReport, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    relay = await target(request, body, session)
    await session.execute(
        update(RelayServer)
        .where(RelayServer.id == relay.id)
        .values(
            health_status=body.status,
            health_detail=body.detail,
            health_checked_at=datetime.now(UTC),
            health_latency_ms=body.latency_ms,
            health_fail_count=0 if body.status == "healthy" else RelayServer.health_fail_count + 1,
        )
    )
    await session.commit()
    return {"success": True, "code": 0, "data": None}


@router.get("/api/infra/relay/servers", dependencies=[Depends(RELAY_SCOPE)])
@router.get(
    "/api/robot/clawrelay-servers", dependencies=[Depends(RELAY_SCOPE)], include_in_schema=False
)
async def servers(
    host: str | None = Query(default=None, max_length=253),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(RelayServer).where(RelayServer.is_active).order_by(RelayServer.name)
    if host:
        stmt = stmt.where(RelayServer.host == host)
    catalog = await load_catalog(session)
    data = []
    for r in (await session.execute(stmt)).scalars():
        item = {
            key: getattr(r, key)
            for key in (
                "name",
                "host",
                "ssh_user",
                "runtime_env",
                "chroot_path",
                "runtime_user",
                "clawrelay_port",
                "agent_port",
                "model_provider",
                "supported_models_mode",
                "description",
                "is_active",
                "health_status",
                "health_checked_at",
                "health_detail",
                "health_latency_ms",
                "health_fail_count",
                "rate_limit_5h_used_pct",
                "rate_limit_5h_resets_at",
                "rate_limit_7d_used_pct",
                "rate_limit_7d_resets_at",
                "rate_limit_probed_at",
            )
        }
        item.update(
            id=str(r.id),
            relay_url=r.relay_url,
            webhook_port=r.agent_port,
            configured_supported_models=r.supported_models,
            effective_supported_models=effective_models(r, catalog),
            supported_models=effective_models(r, catalog),
            team=str(r.team_id) if r.team_id else None,
        )
        data.append(item)
    return {"success": True, "code": 0, "data": data}
