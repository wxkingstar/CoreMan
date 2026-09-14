"""成本只按完整用量和当日有效价格估算，缺失即未知。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ModelPrice
from coreman.core.relay.models import backend_of


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
    day = at.astimezone(UTC).date()
    price = await session.scalar(
        select(ModelPrice)
        .where(
            ModelPrice.provider == backend_of(model),
            ModelPrice.model == model,
            ModelPrice.effective_from <= day,
        )
        .order_by(ModelPrice.effective_from.desc())
        .limit(1)
    )
    if price is None:
        return None
    rates = [price.input_usd, price.output_usd, price.cache_read_usd, price.cache_write_usd]
    value = sum(
        (Decimal(count or 0) * rate for count, rate in zip(counts, rates, strict=True)), Decimal(0)
    ) / Decimal(1000000)
    return value.quantize(Decimal("0.000001")) if value <= Decimal("999999.999999") else None
