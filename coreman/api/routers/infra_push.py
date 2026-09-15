"""主动推送只入 outbox；幂等重试不重复发送。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.infra_auth import require_scope
from coreman.core.bus import outbox
from coreman.core.db.models import ApiClient, Bot, OutboxItem

router = APIRouter(tags=["infra-push"])
PUSH = require_scope("push")


class PushIn(BaseModel):
    bot_key: str = Field(min_length=1, max_length=50)
    chat_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20480)
    msgtype: Literal["markdown"] = "markdown"
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def byte_limit(cls, value: str) -> str:
        if not value.strip() or len(value.encode()) > 20480:
            raise ValueError("正文须非空且不超过 20480 字节")
        return value


@router.post("/api/infra/push")
@router.post("/api/push")
async def push(
    body: PushIn,
    client: ApiClient = Depends(PUSH),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = (
        await session.execute(select(Bot).where(Bot.bot_key == body.bot_key))
    ).scalar_one_or_none()
    if bot is None:
        raise not_found("机器人不存在")
    if not bot.enabled:
        raise ApiError(409, 409, "机器人已停用")
    # request_id 属于签名正文，不能用未签名的 header 覆盖它。
    key = f"push:{client.app_key}:{body.request_id or uuid.uuid4().hex}"
    target, payload = {"chat_id": body.chat_id}, {"markdown": body.content}
    item = await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=key,
        target=target,
        payload=payload,
    )
    if item is None:
        item = (
            await session.execute(select(OutboxItem).where(OutboxItem.dedupe_key == key))
        ).scalar_one()
        if item.bot_id != bot.id or item.target != target or item.payload != payload:
            raise ApiError(409, 409, "request_id 已用于不同的推送内容")
    await session.commit()
    return {
        "code": 0,
        "status": "queued",
        "message": "推送已入队",
        "data": {"outbox_id": item.id, "status": item.status},
    }
