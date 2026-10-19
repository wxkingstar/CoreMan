"""实例订阅额度：节点 Agent 定时上报与对话收尾时随响应带回，两条路径写同一组列。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer


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


def parse_quota(value: Any) -> Quota | None:
    """解析运行时带回的额度；格式不对时返回 None，不让一轮对话因此失败。"""
    try:
        return Quota.model_validate(value)
    except ValidationError:
        return None


async def save_quota(session: AsyncSession, relay_id: uuid.UUID, quota: Quota) -> None:
    """写入额度与采集时间；调用方负责提交。"""
    values: dict[str, Any] = {"rate_limit_probed_at": datetime.now(UTC)}
    windows = (quota.five_hour, quota.seven_day)
    # 空额度只是心跳，保留上次采集值（旧 git_webhook 在额度耗尽时会发 {}）。
    if any(windows):
        for label, window in zip(("5h", "7d"), windows, strict=True):
            values[f"rate_limit_{label}_used_pct"] = window.used_percentage if window else None
            values[f"rate_limit_{label}_resets_at"] = window.resets_at if window else None
    await session.execute(update(RelayServer).where(RelayServer.id == relay_id).values(**values))
