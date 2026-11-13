"""个人内容的访问记录：运行时路径之外的每一次解密都经这里（「我的助理」页的读取）。

本层只写一条结构化应用日志；3D 把 `record` 的函数体换成写 `assistant_access_log` 表，调用点
不动（所以这里已经收下 `session`）。记录里只有编号、动作与对象的代号：`target` 只能是明文的
编号或范围名（`F012`、子任务编号、`open`），知识页的路径本身是个人内容，调用方不要传进来。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.logging import get_logger

log = get_logger(__name__)

ACTIONS = (
    "approvals.read",
    "followups.read",
    "jobs.read",
    "notes.index",
    "notes.read",
    "settings.read",
)
MAX_TARGET = 64


async def record(
    session: AsyncSession,
    bot_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    target: str = "",
) -> None:
    """记一次读取。调用方在返回内容之前调用；与读取在同一个请求里。

    Args:
        session: 这次请求的会话（3D 用它落库；本层不用）
        action: `ACTIONS` 之一
        target: 明文的编号或范围名，超长截断

    Raises:
        ValueError: 不认识的动作（写错的调用点要在测试里就失败）
    """
    del session
    if action not in ACTIONS:
        raise ValueError(f"unknown access action: {action}")
    log.info(
        "assistant_content_access",
        bot_id=str(bot_id),
        actor_id=str(actor_id),
        action=action,
        target=target[:MAX_TARGET],
    )
