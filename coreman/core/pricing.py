"""成本只按完整用量和当日有效价格估算，缺失即未知。

价格来自两处：代码里的内置官方价（`price_list.BUILTIN_PRICES`）和管理员录入的 `model_prices`，
同一模型同一生效日以管理员录入的为准。写日志时固定成本快照，日后改价不回写已有成本；
`backfill_costs` 只补算还没有成本的记录。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import distinct, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import CHAT_LOG_RUNNING, ChatLog, CronRun, ModelPrice
from coreman.core.logging import get_logger
from coreman.core.price_list import BUILTIN_PRICES, canonical_model

_MILLION = Decimal(1000000)
# cost_usd 是 Numeric(12, 6)。
_MAX_COST = Decimal("999999.999999")
_TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

Source = Literal["builtin", "custom"]


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    effective_from: date
    input_usd: Decimal
    output_usd: Decimal
    cache_read_usd: Decimal
    cache_write_usd: Decimal
    source: Source

    @property
    def rates(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return (self.input_usd, self.output_usd, self.cache_read_usd, self.cache_write_usd)


def _price(row: Any, source: Source) -> Price:
    return Price(
        provider=row.provider,
        model=row.model,
        effective_from=row.effective_from,
        input_usd=row.input_usd,
        output_usd=row.output_usd,
        cache_read_usd=row.cache_read_usd,
        cache_write_usd=row.cache_write_usd,
        source=source,
    )


Schedule = dict[tuple[str, str], list[Price]]


async def load_schedule(session: AsyncSession) -> Schedule:
    """按 (后端, 规范模型名) 分组、生效日升序的价格；同日管理员录入覆盖内置价。"""
    merged: dict[tuple[str, str], dict[date, Price]] = defaultdict(dict)
    for builtin in BUILTIN_PRICES:
        merged[canonical_model(builtin.model)][builtin.effective_from] = _price(builtin, "builtin")
    for row in await session.scalars(select(ModelPrice)):
        merged[canonical_model(row.model)][row.effective_from] = _price(row, "custom")
    return {key: [days[day] for day in sorted(days)] for key, days in merged.items()}


def price_on(schedule: Schedule, model: str, day: date) -> Price | None:
    found = None
    for price in schedule.get(canonical_model(model), ()):
        if price.effective_from > day:
            break
        found = price
    return found


def cost_of(price: Price, counts: list[int]) -> Decimal | None:
    value = (
        sum(
            (Decimal(count) * rate for count, rate in zip(counts, price.rates, strict=True)),
            Decimal(0),
        )
        / _MILLION
    )
    return value.quantize(Decimal("0.000001")) if value <= _MAX_COST else None


async def estimate(
    session: AsyncSession,
    *,
    model: str | None,
    at: datetime,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
) -> Decimal | None:
    counts = [input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens]
    if not model or any(value is None or value < 0 for value in counts):
        return None
    price = price_on(await load_schedule(session), model, at.astimezone(UTC).date())
    return None if price is None else cost_of(price, [value or 0 for value in counts])


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


async def backfill_costs(session: AsyncSession) -> int:
    """给已结束、四类用量齐全但还没有成本的记录补算成本；返回补算的对话记录条数。

    已有成本的记录不动（写入时的快照）。定时执行记录按 task_id 跟随对应的对话记录。
    调用方负责提交。
    """
    schedule = await load_schedule(session)
    pending = [
        ChatLog.cost_usd.is_(None),
        ChatLog.status != CHAT_LOG_RUNNING,
        *(getattr(ChatLog, key) >= 0 for key in _TOKENS),
    ]
    models = await session.scalars(
        select(distinct(ChatLog.model)).where(ChatLog.model.is_not(None), *pending)
    )
    filled = 0
    for model in [name for name in models if name]:
        periods = schedule.get(canonical_model(model), [])
        for index, price in enumerate(periods):
            terms = [
                getattr(ChatLog, key) * rate for key, rate in zip(_TOKENS, price.rates, strict=True)
            ]
            cost = (terms[0] + terms[1] + terms[2] + terms[3]) / _MILLION
            window = [ChatLog.request_at >= _day_start(price.effective_from)]
            if index + 1 < len(periods):
                window.append(ChatLog.request_at < _day_start(periods[index + 1].effective_from))
            result = await session.execute(
                update(ChatLog)
                .where(ChatLog.model == model, *pending, *window, cost <= _MAX_COST)
                .values(cost_usd=cost)
                .execution_options(synchronize_session=False)
            )
            filled += result.rowcount or 0  # type: ignore[attr-defined]
    await session.execute(
        update(CronRun)
        .where(
            CronRun.cost_usd.is_(None),
            CronRun.task_id == ChatLog.task_id,
            ChatLog.cost_usd.is_not(None),
        )
        .values(cost_usd=ChatLog.cost_usd)
        .execution_options(synchronize_session=False)
    )
    return filled


async def backfill_on_startup(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """发布后内置价目表可能有变：补算一次。多个 API 实例同时启动时只有一个执行。"""
    log = get_logger(__name__)
    try:
        async with session_factory() as session:
            if not await session.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('coreman-cost-backfill', 0))"
                )
            ):
                return
            filled = await backfill_costs(session)
            await session.commit()
        log.info("cost_backfill_done", filled=filled)
    except Exception:
        # 补算失败不影响服务；下次启动或下次改价会再补。
        log.exception("cost_backfill_failed")
