"""Bounded, timestamped observations; legacy health never proves model inference."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer, RuntimeNode


class HealthObservation(BaseModel):
    status: Literal["healthy", "down", "auth_fail", "timeout", "unknown"]
    detail: str | None = Field(default=None, max_length=255)
    latency_ms: int | None = Field(default=None, ge=0, le=3600000)
    check_kind: Literal["legacy", "driver", "model", "conversation"] = "legacy"
    observed_at: datetime | None = None
    model: str | None = Field(default=None, max_length=150)
    requested_model: str | None = Field(default=None, max_length=150)
    reply_verified: StrictBool = False
    driver_status: Literal["healthy", "down", "timeout", "unknown"] | None = None
    seat_id: str | None = Field(default=None, pattern=r"^(default|[a-f0-9]{12})$")
    error_code: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9_]+$")

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
            if value > datetime.now(UTC) + timedelta(seconds=30):
                raise ValueError("observation timestamp is in the future")
        return value


def observation_time(value: dict[str, Any] | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str((value or {}).get("observed_at"))).astimezone(UTC)
    except (TypeError, ValueError):
        return None


async def save_health(
    session: AsyncSession, relay_id: uuid.UUID, observation: HealthObservation
) -> None:
    """Serialize writers and reject out-of-order results without changing config version."""
    relay = await session.scalar(
        select(RelayServer).where(RelayServer.id == relay_id).with_for_update()
    )
    if relay is None:
        return
    if not await matches_active_seat(session, relay, observation.seat_id):
        return
    at = observation.observed_at or datetime.now(UTC)
    data = observation.model_dump(mode="json")
    if (
        observation.check_kind == "model"
        and observation.status in ("down", "auth_fail", "timeout")
        and not observation.error_code
        and observation.detail
    ):
        _, code, _ = provider_failure(observation.detail)
        if code != "model_request_failed":
            data["error_code"] = code
    data["observed_at"] = at.isoformat()
    data["reply_verified"] = bool(
        observation.reply_verified
        and observation.check_kind in ("model", "conversation")
        and observation.status == "healthy"
    )
    values: dict[str, Any] = {}
    if observation.check_kind == "model" and observation.driver_status is not None:
        before_driver = observation_time(relay.driver_health)
        if before_driver is None or before_driver <= at:
            values["driver_health"] = HealthObservation(
                status=observation.driver_status,
                check_kind="driver",
                observed_at=at,
                seat_id=observation.seat_id,
            ).model_dump(mode="json")
    field = {
        "driver": "driver_health",
        "model": "model_health",
        "conversation": "model_health",
    }.get(observation.check_kind)
    if field:
        before = observation_time(getattr(relay, field))
        if before is not None and before > at:
            return
        values[field] = data
        if field == "model_health" and observation.status in ("down", "auth_fail", "timeout"):
            values["last_model_failure"] = data
    if relay.health_checked_at is None or relay.health_checked_at <= at:
        values.update(
            health_status=observation.status,
            health_detail=observation.detail,
            health_checked_at=at,
            health_latency_ms=observation.latency_ms,
            health_fail_count=0
            if observation.status == "healthy"
            else RelayServer.health_fail_count + 1,
        )
    if values:
        await session.execute(
            update(RelayServer).where(RelayServer.id == relay_id).values(**values)
        )


def telemetry_view(relay: RelayServer) -> dict[str, Any]:
    """Old rows retain legacy fields; absent typed observations remain absent."""
    return {
        "driver_health": relay.driver_health,
        "model_health": relay.model_health,
        "last_model_failure": relay.last_model_failure,
        "quota_observation": relay.quota_observation
        or (
            {
                "read_status": "unsupported",
                "error_code": "provider_quota_not_integrated",
                "windows": {},
            }
            if relay.model_provider == "opencode"
            else None
        ),
    }


async def matches_active_seat(
    session: AsyncSession, relay: RelayServer, seat_id: str | None
) -> bool:
    # Legacy runtime releases advertise seats but omit seat_id in telemetry.
    # Only an explicitly mismatched seat is rejected during API-first rollout.
    if seat_id is None:
        return True
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    seats = (
        ((node.capabilities or {}).get(relay.model_provider) or {}).get("seats", []) if node else []
    )
    active = next((s.get("id") for s in seats if s.get("active")), None)
    # Explicit provenance from a retired seat cannot repaint the active seat.
    return active is None or seat_id == active


def provider_failure(message: str) -> tuple[str, str, str]:
    """Classify diagnostics without persisting a prompt or arbitrary provider body."""
    text = message.lower()
    for markers, status, code, detail in (
        (
            ("account is on hold", "account_on_hold", "claude.ai/restricted"),
            "auth_fail",
            "account_restricted",
            "账号受限或被暂停使用",
        ),
        (
            ("session has expired", "session_expired", "oauth session expired"),
            "auth_fail",
            "session_expired",
            "登录会话已过期，请重新登录",
        ),
        (
            ("usage limit", "quota exceeded"),
            "down",
            "quota_limited",
            "已触发订阅额度限制",
        ),
        (
            ("too many requests", "rate limit"),
            "down",
            "rate_limited",
            "模型服务短时限流",
        ),
        (
            ("not logged in", "authentication_error", "invalid api key", "401", "403"),
            "auth_fail",
            "authentication_error",
            "模型服务认证失败",
        ),
    ):
        if any(marker in text for marker in markers):
            return status, code, detail
    return "down", "model_request_failed", "模型调用失败"
