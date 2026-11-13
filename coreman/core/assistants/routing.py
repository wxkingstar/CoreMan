"""入站路由：一个助理应用下，按所有者找他本人那条启用的助理记录。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot


async def assistant_for(session: AsyncSession, app_id: uuid.UUID, user_id: uuid.UUID) -> Bot | None:
    """`app_id` 是 platform_apps.id，`user_id` 是 users.id；没有或已停用返回 None。

    一人同时只有一条启用的助理记录（`bots_assistant_owner_uq`），所以最多命中一行。
    """
    stmt = select(Bot).where(
        Bot.kind == "assistant",
        Bot.platform_app_id == app_id,
        Bot.owner_id == user_id,
        Bot.enabled.is_(True),
    )
    bot: Bot | None = await session.scalar(stmt)
    return bot
