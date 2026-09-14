"""实例工作目录的分配锁，避免并发创建/换机将文件写入另一个机器人。"""

import uuid
from pathlib import PurePosixPath

from sqlalchemy import select, text
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
    if relay_id is None:
        return
    relay = await session.get(RelayServer, relay_id)
    peers = [relay_id]
    owner = relay_id
    if relay and relay.runtime_node_id:
        node = await session.get(RuntimeNode, relay.runtime_node_id)
        if (
            node is None
            or not PurePosixPath(directory).is_relative_to(node.workspace_root)
            or str(PurePosixPath(directory)) == node.workspace_root
        ):
            raise ApiError(422, 422, "工作目录必须位于所选运行时的项目主目录之下")
        owner = node.id
        peers = list(
            await session.scalars(
                select(RelayServer.id).where(RelayServer.runtime_node_id == node.id)
            )
        )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace:{owner}:{directory}"},
    )
    stmt = select(Bot.id).where(Bot.relay_server_id.in_(peers), Bot.working_dir == directory)
    if bot_id is not None:
        stmt = stmt.where(Bot.id != bot_id)
    if await session.scalar(stmt.limit(1)):
        raise ApiError(409, 409, "该实例工作目录已属于另一个机器人")
