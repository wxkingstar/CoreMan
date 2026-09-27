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


async def private_chat(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID | None, chat_id: str
) -> bool:
    """这个会话是不是这位成员与本机器人的私聊。

    只认真实私聊消息留下的记录（`user_reached` 由私聊消息那一轮写入，卡片按钮点出来的轮次
    不写）；飞书的卡片回调不带会话类型，点击按私聊还是按群处理只看这里。
    """
    if user_id is None or not chat_id:
        return False
    reached = await session.get(UserReached, (bot_id, user_id), populate_existing=True)
    return reached is not None and reached.platform_chat_id == chat_id


async def private_chat_of(
    session: AsyncSession, bot_id: uuid.UUID, platform: str, platform_user_id: str, chat_id: str
) -> bool:
    """同上，按平台 user_id 找成员（网关应答回调时只有这个）。"""
    if not platform_user_id:
        return False
    user_id = await session.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.platform == platform, UserIdentity.platform_user_id == platform_user_id
        )
    )
    return await private_chat(session, bot_id, user_id, chat_id)
