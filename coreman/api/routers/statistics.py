"""可见范围内的日统计和模型价格，不将未知用量、未知价格伪装成零。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden
from coreman.api.routers.chat_logs import _scope, accessible_bot_ids
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.db.models import Bot, ChatLog, ModelPrice, User
from coreman.core.relay.models import backend_of

router = APIRouter(prefix="/api/admin", tags=["statistics"], dependencies=[Depends(verify_csrf)])
TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def aggregates() -> list[Any]:
    fields: list[Any] = [
        func.count().label("messages"),
        func.count(func.distinct(ChatLog.user_id)).label("users"),
        func.count(func.distinct(ChatLog.bot_id)).label("bots"),
    ]
    for key in (*TOKENS, "cost_usd"):
        col = getattr(ChatLog, key)
        fields.extend([func.sum(col).label(key), func.count(col).label(f"{key}_measured")])
    return fields


def aggregate_out(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in (*TOKENS, "cost_usd"):
        value = data[key]
        data[key] = float(value) if key == "cost_usd" and value is not None else value
    return data


@router.get("/statistics")
async def statistics(
    request: Request,
    start: date | None = None,
    end: date | None = None,
    timezone: Literal["UTC", "Asia/Shanghai", "Asia/Tokyo"] = "Asia/Shanghai",
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    end = end or datetime.now(zone).date()
    start = start or end - timedelta(days=29)
    if start > end or (end - start).days > 365:
        raise ApiError(422, 422, "统计范围须为 1 至 366 天")
    lower = datetime.combine(start, time.min, zone).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC)
    ids = await accessible_bot_ids(session, actor)
    conditions = _scope(ids, actor, bot_token=bool(request.cookies.get("bot_token"))) + [
        ChatLog.request_at >= lower,
        ChatLog.request_at < upper,
    ]
    # 每条聚合查询有上限；统计不应挤占实时聊天的数据库资源。
    await session.execute(text("SELECT set_config('statement_timeout', '5000', true)"))
    total = (await session.execute(select(*aggregates()).where(*conditions))).mappings().one()
    day = func.date(func.timezone(timezone, ChatLog.request_at))
    daily = (
        (
            await session.execute(
                select(day.label("day"), *aggregates())
                .where(*conditions)
                .group_by(day)
                .order_by(day)
            )
        )
        .mappings()
        .all()
    )
    by_bot = (
        (
            await session.execute(
                select(
                    ChatLog.bot_id.label("id"),
                    func.max(Bot.name).label("name"),
                    *aggregates(),
                )
                .where(*conditions)
                .outerjoin(Bot, Bot.id == ChatLog.bot_id)
                .group_by(ChatLog.bot_id)
                .order_by(func.count().desc(), ChatLog.bot_id)
                .limit(50)
            )
        )
        .mappings()
        .all()
    )
    by_user = (
        (
            await session.execute(
                select(
                    ChatLog.user_id.label("id"),
                    func.max(ChatLog.user_name).label("name"),
                    *aggregates(),
                )
                .where(*conditions, ChatLog.user_id.is_not(None))
                .group_by(ChatLog.user_id)
                .order_by(func.count().desc(), ChatLog.user_id)
                .limit(50)
            )
        )
        .mappings()
        .all()
    )
    return {
        "code": 0,
        "data": {
            "start": start,
            "end": end,
            "timezone": timezone,
            "total": aggregate_out(total),
            "daily": [aggregate_out(row) for row in daily],
            "by_bot": [aggregate_out(row) for row in by_bot],
            "by_user": [aggregate_out(row) for row in by_user],
            "cost_type": "estimate",
            "ranking_limit": 50,
        },
    }


class PriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["claude", "codex"]
    model: str = Field(min_length=1, max_length=100)
    effective_from: date
    input_usd: Decimal = Field(ge=0, le=99999, decimal_places=4)
    output_usd: Decimal = Field(ge=0, le=99999, decimal_places=4)
    cache_read_usd: Decimal = Field(ge=0, le=99999, decimal_places=4)
    cache_write_usd: Decimal = Field(ge=0, le=99999, decimal_places=4)

    @model_validator(mode="after")
    def provider_matches(self) -> PriceIn:
        if self.provider != backend_of(self.model):
            raise ValueError("模型与后端不一致")
        return self


def price_out(row: ModelPrice) -> dict[str, Any]:
    return {"version": row.version, **{key: getattr(row, key) for key in PriceIn.model_fields}}


@router.get("/model-prices")
async def prices(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = await session.scalars(
        select(ModelPrice).order_by(ModelPrice.model, ModelPrice.effective_from.desc()).limit(2000)
    )
    return {"code": 0, "data": [price_out(row) for row in rows]}


@router.put("/model-prices")
async def put_price(
    body: PriceIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in ("ai_committee", "platform_admin"):
        raise forbidden()
    row = await session.get(ModelPrice, (body.provider, body.model, body.effective_from))
    require_if_match(request, row.version if row else 0)
    before = price_out(row) if row else None
    if row is None:
        row = ModelPrice(**body.model_dump())
        session.add(row)
    else:
        for key, value in body.model_dump().items():
            setattr(row, key, value)
    await session.flush()
    await record_audit(
        session,
        action="model_price.save",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="model_price",
        target_id=f"{body.provider}:{body.model}:{body.effective_from}",
        diff={"price": [str(before), str(price_out(row))]},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": price_out(row)}
