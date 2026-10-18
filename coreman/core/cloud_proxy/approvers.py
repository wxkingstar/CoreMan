"""机器人的云服务写操作审批人。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import BotApprover, User


async def approver_logins(session: AsyncSession, bot_id: uuid.UUID) -> list[str]:
    # 与成员可用性口径一致：停用账号和引导管理员不算审批人。
    rows = await session.scalars(
        select(User.login_name)
        .join(BotApprover, BotApprover.user_id == User.id)
        .where(
            BotApprover.bot_id == bot_id,
            User.status == "active",
            User.source != "bootstrap",
            User.login_name.is_not(None),
        )
        .order_by(User.login_name)
    )
    return [login for login in rows if login]
