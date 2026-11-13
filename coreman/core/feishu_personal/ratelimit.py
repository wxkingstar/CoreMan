"""以所有者身份调飞书的限频：认出限频应答，按应用记用量。

飞书按「应用 × 企业 × 接口」限频，全员共用一个助理应用：列会话与读会话历史各每分钟 1000 次。
盯消息的轮询循环每轮先看最近 60 秒的用量，接近上限就拉长所有人的间隔，满了就跳过这一轮；
助理工具的调用只记不拦。用量是流水（一次调用一行）：计数器的行锁会被调用方的事务占着。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import FeishuRateUsage
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.timeutils import aware_utc

RATE_LIMIT_CODE = 99991400
CHATS, HISTORY = "chats", "history"
# 每分钟的上限（官方文档；见 spec「前置验证」）。
LIMITS: dict[str, int] = {CHATS: 1000, HISTORY: 1000}
WINDOW = timedelta(seconds=60)
RETENTION = timedelta(minutes=15)
DEFAULT_RETRY, MAX_RETRY = 60, 600
# 用量到这个比例算「接近上限」：开始拉长间隔，并打告警日志。
TIGHT = 0.6
# 应答头里的等待秒数：飞书网关的头在前，标准的 Retry-After 兜底。
_RESET_HEADERS = ("x-ogw-ratelimit-reset", "retry-after")
_CATEGORIES = {("GET", "/im/v1/chats"): CHATS, ("GET", "/im/v1/messages"): HISTORY}


class RateLimited(PersonalError):
    """飞书说调得太频繁。只带等待秒数与接口类别，不带上游的任何文字。"""

    def __init__(self, retry_after: int, category: str | None = None) -> None:
        super().__init__("rate_limited", upstream_code=RATE_LIMIT_CODE)
        self.retry_after = max(1, min(MAX_RETRY, int(retry_after)))
        self.category = category

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "retry_after": self.retry_after}


def retry_after(headers: Mapping[str, str]) -> int:
    """应答头里说的等待秒数；读不到按 `DEFAULT_RETRY`。"""
    for name in _RESET_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = int(float(raw))
        except ValueError:
            continue
        if seconds > 0:
            return min(MAX_RETRY, seconds)
    return DEFAULT_RETRY


def detect(status: int, data: Any, headers: Mapping[str, str]) -> RateLimited | None:
    """这个应答是不是限频：HTTP 429，或正文里的错误码 99991400。"""
    code = data.get("code") if isinstance(data, dict) else None
    if status == 429 or code == RATE_LIMIT_CODE:
        return RateLimited(retry_after(headers))
    return None


def category(endpoint: endpoints.Endpoint) -> str | None:
    """这个接口计入哪一类分钟用量；不计的返回 None。"""
    return _CATEGORIES.get((endpoint.method, endpoint.path))


def stretch(load: float) -> int | None:
    """用量占比对应的轮询间隔倍数；None 表示额度已满，这一轮不调。"""
    if load >= 1.0:
        return None
    if load >= 0.8:
        return 4
    return 2 if load >= TIGHT else 1


@dataclass(frozen=True)
class Budget:
    """一个应用的分钟用量。各方法都不提交。"""

    app_id: str

    async def record(
        self,
        session: AsyncSession,
        category: str,
        *,
        at: datetime | None = None,
        units: int = 1,
    ) -> None:
        values: dict[str, Any] = {"app_id": self.app_id, "category": category, "units": units}
        if at is not None:
            values["at"] = aware_utc(at)
        await session.execute(insert(FeishuRateUsage).values(**values))

    async def used(self, session: AsyncSession, now: datetime) -> dict[str, int]:
        """最近 60 秒各类用了多少。上界留给 `exhaust` 写在未来的那一笔。"""
        now = aware_utc(now)
        rows = await session.execute(
            select(FeishuRateUsage.category, func.coalesce(func.sum(FeishuRateUsage.units), 0))
            .where(
                FeishuRateUsage.app_id == self.app_id,
                FeishuRateUsage.at > now - WINDOW,
                FeishuRateUsage.at <= now + timedelta(seconds=MAX_RETRY),
            )
            .group_by(FeishuRateUsage.category)
        )
        found = {name: int(total) for name, total in rows.all()}
        return {name: found.get(name, 0) for name in LIMITS}

    async def load(self, session: AsyncSession, now: datetime) -> float:
        """两类里用量占上限比例的较大者。"""
        used = await self.used(session, now)
        return max(used[name] / LIMITS[name] for name in LIMITS)

    async def exhaust(
        self, session: AsyncSession, category: str, now: datetime, retry_after: int
    ) -> None:
        """飞书限频了：记一笔占满额度、恰好在等待结束时滑出窗口的用量，同一应用的人都先停下。"""
        at = aware_utc(now) + timedelta(seconds=retry_after) - WINDOW
        await self.record(session, category, at=at, units=LIMITS[category])


async def purge(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        delete(FeishuRateUsage).where(FeishuRateUsage.at < aware_utc(now) - RETENTION)
    )
    return int(getattr(result, "rowcount", 0) or 0)
