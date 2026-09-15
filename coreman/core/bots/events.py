"""机器人配置变更通知（spec §6.3 pg_notify 通道）。API 路由与 worker 共用。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.notify import notify


async def notify_bot_changed(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """让网关/工作进程重载这台机器人的配置。

    与写操作同事务：commit 之前发出的 pg_notify 只有在事务真的提交后才会送达，回滚了就当
    没发生过（bots_extra 复用）。
    """
    await notify(session, "config_changed", {"table": "bots", "id": str(bot_id)})
