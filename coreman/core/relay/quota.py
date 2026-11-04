"""实例订阅额度：节点 Agent 定时上报与对话收尾时随响应带回，两条路径写同一组列。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer
from coreman.core.relay.observations import matches_active_seat


class QuotaWindow(BaseModel):
    used_percentage: float | None = Field(default=None, ge=0, le=100)
    resets_at: datetime | None = None
    observed_at: datetime | None = None

    @field_validator("resets_at", "observed_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class Quota(BaseModel):
    five_hour: QuotaWindow | None = None
    seven_day: QuotaWindow | None = None
    plan_type: str | None = Field(default=None, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    account_verified: StrictBool | None = None
    seat_id: str | None = Field(default=None, pattern=r"^(default|[a-f0-9]{12})$")
    observed_at: datetime | None = None
    source: Literal["conversation", "probe", "driver_cache", "unknown"] = "unknown"
    read_status: Literal["observed", "no_data", "unsupported", "error"] = "observed"
    error_code: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9_]+$")
    window_applicability: dict[
        Literal["five_hour", "seven_day"], Literal["applicable", "not_applicable", "unknown"]
    ] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def observed_time(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def parse_quota(value: Any) -> Quota | None:
    """解析运行时带回的额度；格式不对时返回 None，不让一轮对话因此失败。"""
    try:
        return Quota.model_validate(value)
    except ValidationError:
        return None


async def save_quota(session: AsyncSession, relay_id: uuid.UUID, quota: Quota) -> None:
    """A failed/empty read never repaints old quota as fresh. Each window owns its time."""
    relay = await session.scalar(
        select(RelayServer).where(RelayServer.id == relay_id).with_for_update()
    )
    if relay is None:
        return
    if not await matches_active_seat(session, relay, quota.seat_id):
        return
    now = datetime.now(UTC)
    at = quota.observed_at or now
    if at > now + timedelta(seconds=30):
        return
    previous = dict(relay.quota_observation or {})
    previous_attempt = previous.get("observed_at")
    if previous_attempt and datetime.fromisoformat(previous_attempt) > at:
        return
    metadata = {
        **previous,
        "observed_at": at.isoformat(),
        "last_attempt_at": now.isoformat(),
        "source": quota.source,
        "read_status": quota.read_status,
        "error_code": quota.error_code,
    }
    if quota.plan_type is not None:
        metadata["plan_type"] = quota.plan_type
    if quota.account_verified is not None:
        metadata["account_verified"] = quota.account_verified
    windows = dict(previous.get("windows") or {})
    for key, native in (("five_hour", "5h"), ("seven_day", "7d")):
        old = getattr(relay, f"rate_limit_{native}_used_pct")
        reset = getattr(relay, f"rate_limit_{native}_resets_at")
        if key not in windows and old is not None:
            windows[key] = {
                "used_percentage": float(old),
                "resets_at": reset.isoformat() if reset else None,
                "observed_at": relay.rate_limit_probed_at.isoformat()
                if relay.rate_limit_probed_at
                else None,
                "applicability": "unknown",
                "status": "legacy",
            }
    applicability = {str(key): str(value) for key, value in quota.window_applicability.items()}
    # Plan labels and missing percentages are not upstream window policy.
    # Only explicit window_applicability can mark a window not_applicable.
    values: dict[str, Any] = {}
    successful_times = []
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = getattr(quota, name)
        used = window.used_percentage if window else None
        if used is not None:
            observed = window.observed_at or at
            if observed > now + timedelta(seconds=30):
                continue
            prior = windows.get(name) or {}
            if prior.get("observed_at") and datetime.fromisoformat(prior["observed_at"]) > observed:
                continue
            windows[name] = {
                "used_percentage": used,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
                "observed_at": observed.isoformat(),
                "applicability": "applicable",
                "status": "observed",
            }
            successful_times.append(observed)
            values[f"rate_limit_{label}_used_pct"] = used
            values[f"rate_limit_{label}_resets_at"] = window.resets_at
        elif applicability.get(name) == "not_applicable":
            windows[name] = {
                "used_percentage": None,
                "resets_at": None,
                "observed_at": at.isoformat(),
                "applicability": "not_applicable",
                "status": "not_applicable",
            }
            values[f"rate_limit_{label}_used_pct"] = None
            values[f"rate_limit_{label}_resets_at"] = None
        else:
            # Retain historical figures with their original time. Legacy
            # numeric columns are cleared on a partial successful report so
            # their shared timestamp cannot make a missing window look fresh.
            prior = dict(windows.get(name) or {})
            prior.update(
                status="not_provided",
                applicability=applicability.get(name, prior.get("applicability", "unknown")),
            )
            windows[name] = prior
            if quota.five_hour or quota.seven_day:
                values[f"rate_limit_{label}_used_pct"] = None
                values[f"rate_limit_{label}_resets_at"] = None
    if successful_times:
        latest = max(successful_times)
        previous_success = previous.get("last_success_at")
        metadata["last_success_at"] = max(
            latest, datetime.fromisoformat(previous_success) if previous_success else latest
        ).isoformat()
        # Old consumers have only one timestamp. Use the oldest retained
        # numeric window, so a new weekly read cannot repaint an old 5h value.
        legacy_times = [
            datetime.fromisoformat(windows[name]["observed_at"])
            for name, label in (("five_hour", "5h"), ("seven_day", "7d"))
            if values.get(
                f"rate_limit_{label}_used_pct", getattr(relay, f"rate_limit_{label}_used_pct")
            )
            is not None
            and windows.get(name, {}).get("observed_at")
        ]
        values["rate_limit_probed_at"] = min(legacy_times) if legacy_times else latest
    if not successful_times and not applicability and quota.read_status == "observed":
        metadata["read_status"] = "no_data"
    metadata["windows"] = windows
    values["quota_observation"] = metadata
    await session.execute(update(RelayServer).where(RelayServer.id == relay_id).values(**values))
