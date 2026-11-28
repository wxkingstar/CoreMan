"""在承接员工所在的运行时节点上选后端、跑一轮。

抽取和编译都只在这个节点上跑，不换到别的节点。后端先 Claude 再 Codex，用该后端在模型目录里
的默认模型；额度超过阈值的不选，把余量留给调度回合（它们共用同一个运行时的额度）。每一轮带回
的额度当场记到运行时上，下一批开始前看到的就是刚用掉之后的数。

这里的一轮不经聊天管线：没有会话、不挂技能、不带环境变量和业务令牌，平台直接向节点发一次
对话请求，读完正文就结束。
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import Bot, RelayServer
from coreman.core.logging import get_logger
from coreman.core.relay import probe
from coreman.core.relay.client import ChatRequest, RelayError
from coreman.core.relay.models import default_model, effective_models, load_catalog
from coreman.core.relay.quota import parse_quota, save_quota
from coreman.core.relay.sse import (
    FinishEvent,
    RateLimitEvent,
    RelayErrorEvent,
    RuntimeObservationEvent,
    TextDelta,
)
from coreman.core.timeutils import aware_utc

log = get_logger(__name__)

PROVIDERS = ("claude", "codex")
DEFAULT_QUOTA = {"seven_day": 70, "five_hour": 80}
FINISHED = frozenset({"stop", "end_turn", "completed"})
OUTPUT_LIMIT = 400_000


class RoundError(Exception):
    """这一轮没有跑完：节点、驱动或额度的问题。`str()` 可以写进运行记录。"""


@dataclass(frozen=True)
class Backend:
    relay: RelayServer
    provider: str
    model: str


def used(pct: Decimal | None, resets_at: datetime | None, now: datetime) -> float:
    """额度窗口的已用百分比；窗口已经重置（或不知道）按 0 算。"""
    if pct is None or (resets_at is not None and aware_utc(resets_at) <= now):
        return 0.0
    return float(pct)


def within_quota(relay: RelayServer, quota: dict[str, Any], now: datetime) -> bool:
    seven = float(quota.get("seven_day", DEFAULT_QUOTA["seven_day"]))
    five = float(quota.get("five_hour", DEFAULT_QUOTA["five_hour"]))
    return (
        used(relay.rate_limit_7d_used_pct, relay.rate_limit_7d_resets_at, now) < seven
        and used(relay.rate_limit_5h_used_pct, relay.rate_limit_5h_resets_at, now) < five
    )


async def pick(
    session: AsyncSession, bot: Bot, quota: dict[str, Any], now: datetime
) -> tuple[Backend | None, str]:
    """选这次用的后端；选不出时返回 (None, 原因)。"""
    current = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if current is None or current.runtime_node_id is None:
        return None, "承接员工没有分配运行时"
    relays = (
        (
            await session.execute(
                select(RelayServer).where(
                    RelayServer.runtime_node_id == current.runtime_node_id,
                    RelayServer.is_active,
                    RelayServer.assistant_pool.is_(False),
                    RelayServer.model_provider.in_(PROVIDERS),
                )
            )
        )
        .scalars()
        .all()
    )
    catalog = await load_catalog(session)
    reasons: list[str] = []
    for provider in PROVIDERS:
        for relay in sorted(
            (r for r in relays if r.model_provider == provider), key=lambda r: r.name
        ):
            if relay.health_status not in ("healthy", "unknown"):
                reasons.append(f"{relay.name} 不健康")
                continue
            if not within_quota(relay, quota, now):
                reasons.append(f"{relay.name} 额度超过阈值")
                continue
            models = effective_models(relay, catalog)
            preferred = default_model(provider, catalog)
            model = preferred if preferred in models else (models[0] if models else None)
            if model is None:
                reasons.append(f"{relay.name} 没有可用模型")
                continue
            return Backend(relay=relay, provider=provider, model=model), ""
    return None, "；".join(reasons) or "承接员工所在节点没有 Claude 或 Codex 运行时"


async def record_quota(
    factory: async_sessionmaker[AsyncSession], relay_id: Any, limits: dict[str, Any]
) -> None:
    """把一轮带回的额度记到运行时上；格式不对或写不进去都不影响这一轮。"""
    quota = parse_quota(limits)
    if quota is None:
        return
    try:
        async with factory() as session:
            await save_quota(session, relay_id, quota.model_copy(update={"source": "conversation"}))
            await session.commit()
    except Exception:
        log.warning("kb_quota_not_recorded", relay_id=str(relay_id), exc_info=True)


async def run_round(
    backend: Backend,
    *,
    system_prompt: str,
    content: str,
    working_dir: str,
    max_turns: int,
    limit_seconds: float,
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> str:
    """跑一轮，返回全部正文。给了 `factory` 就把这一轮带回的额度记到运行时上（出错的轮次也记）。

    Raises:
        RoundError: 节点不可达、驱动报错、没有正常结束或输出过长
    """
    request = ChatRequest(
        model=backend.model,
        system_prompt=system_prompt,
        user_content=content,
        working_dir=working_dir,
        session_id="",
        backend=backend.provider,
        max_turns=max_turns,
    )
    client = probe.make_client(backend.relay)
    parts: list[str] = []
    size, finished = 0, False
    limits: dict[str, Any] | None = None
    seat: str | None = None
    try:
        async with (
            asyncio.timeout(limit_seconds + 30),
            aclosing(client.chat_stream(request, total_timeout=limit_seconds)) as events,
        ):
            async for event in events:
                if isinstance(event, TextDelta):
                    size += len(event.text)
                    if size > OUTPUT_LIMIT:
                        raise RoundError("输出超过上限")
                    parts.append(event.text)
                elif isinstance(event, RelayErrorEvent):
                    raise RoundError(f"运行时报错：{event.text[:200]}")
                elif isinstance(event, RuntimeObservationEvent):
                    seat = event.seat_id
                elif isinstance(event, RateLimitEvent):
                    limits = event.data
                elif isinstance(event, FinishEvent):
                    finished = event.reason in FINISHED
    except (RelayError, TimeoutError) as exc:
        raise RoundError(str(exc)[:200] or "运行超时") from exc
    finally:
        await client.aclose()
        if factory is not None and limits is not None:
            await record_quota(factory, backend.relay.id, {**limits, "seat_id": seat})
    if not finished:
        raise RoundError("这一轮没有正常结束")
    return "".join(parts)
