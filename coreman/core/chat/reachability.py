"""个人推送只依赖真实私聊记录，并在发送前重新核验接收身份。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import OutboxItem, User, UserIdentity, UserReached


async def private_target_valid(session: AsyncSession, item: OutboxItem) -> bool:
    target = item.target
    if "recipient_user_id" not in target:
        return True
    try:
        user_id = uuid.UUID(target["recipient_user_id"])
    except (ValueError, TypeError):
        return False
    user = await session.get(User, user_id, populate_existing=True)
    if user is None or user.status != "active" or user.source == "bootstrap":
        return False
    reached = await session.get(UserReached, (item.bot_id, user_id))
    if reached is None or reached.platform_chat_id != target.get("chat_id"):
        return False
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id,
            UserIdentity.platform == item.platform,
            UserIdentity.platform_user_id == target.get("recipient_platform_user_id"),
        )
    )
    return identity is not None
