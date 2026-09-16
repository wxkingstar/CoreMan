"""运行时节点上工作目录的分配锁，避免并发创建/换机将文件写入另一个机器人。"""

import uuid
from pathlib import PurePosixPath

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, RelayServer, RuntimeNode
from coreman.core.errors import ApiError


async def reserve_workspace(
    session: AsyncSession,
    relay_id: uuid.UUID | None,
    directory: str,
    *,
    bot_id: uuid.UUID | None = None,
) -> None:
    """目录必须位于实例所属节点的项目主目录之下，且同一节点上只归一个机器人。

    同一节点的 claude / codex 两个实例共用一棵目录树，所以占用检查按节点而不是按实例。
    """
    if relay_id is None:
        return
    relay = await session.get(RelayServer, relay_id)
    node = (
        await session.get(RuntimeNode, relay.runtime_node_id)
        if relay and relay.runtime_node_id
        else None
    )
    if node is None:
        raise ApiError(422, 422, "目标运行时未注册或不可用")
    path = PurePosixPath(directory)
    if not path.is_relative_to(node.workspace_root) or str(path) == node.workspace_root:
        raise ApiError(422, 422, "工作目录必须位于所选运行时的项目主目录之下")
    peers = list(
        await session.scalars(select(RelayServer.id).where(RelayServer.runtime_node_id == node.id))
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace:{node.id}"},
    )
    stmt = select(Bot).where(
        or_(
            Bot.relay_server_id.in_(peers),
            Bot.workspace_target_relay_id.in_(peers),
        )
    )
    if bot_id is not None:
        stmt = stmt.where(Bot.id != bot_id)
    for other in await session.scalars(stmt):
        paths = []
        if other.relay_server_id in peers:
            paths.append(other.working_dir)
        if other.workspace_target_relay_id in peers and other.workspace_target_dir:
            paths.append(other.workspace_target_dir)
        for occupied in paths:
            other_path = PurePosixPath(occupied)
            if path.is_relative_to(other_path) or other_path.is_relative_to(path):
                raise ApiError(409, 409, "工作目录与另一位 AI 员工的目录重叠")
