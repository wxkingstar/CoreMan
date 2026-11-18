"""个人助理权限申请确认卡的回调：核对与建单在 `core.assistants.request_cards.decide`。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import request_cards
from coreman.core.db.models import Bot, InboundEvent
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    return await request_cards.decide(session, ctx.cipher, bot, inbound, action)
