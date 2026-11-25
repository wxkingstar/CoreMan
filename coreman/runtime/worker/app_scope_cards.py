"""「飞书应用缺少权限」卡片的回调：核对与发起扫码在 `core.feishu_apps.scope_prompt.handle`。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, InboundEvent
from coreman.core.feishu_apps import scope_prompt
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    return await scope_prompt.handle(session, ctx.cipher, bot, inbound, action)
