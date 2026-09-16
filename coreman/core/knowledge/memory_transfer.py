"""换机快照与部署分离：调用方可在搬运文件之前持久化已回收记忆。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.tasks import ACTIVE, OPEN
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, Memory, RelayServer, Task
from coreman.core.errors import ApiError
from coreman.core.knowledge.memory import MAX_BATCH_BYTES, MemoryInput, collect
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow


async def ensure_idle(session: AsyncSession, bot: Bot) -> None:
    active = await session.scalar(
        select(Task.id)
        .where(
            Task.bot_id == bot.id,
            (
                (Task.status.in_(ACTIVE) & Task.kind.not_in(["relay_switch"]))
                | ((Task.kind == "skill_install") & Task.status.in_(OPEN))
            ),
        )
        .limit(1)
    )
    if active is not None:
        raise ApiError(409, 409, "请先等待或停止正在执行的任务，再迁移工作目录与记忆")


async def recover(
    session: AsyncSession,
    bot: Bot,
    cipher: Cipher | None,
    *,
    allow_stored: bool = False,
) -> str:
    """回收源快照；离线降级需要显式选择且必须有已回收记录。"""
    old = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if old is None or old.runtime_node_id is None:
        return "deployed_stored"
    try:
        capability = await call_agent(old, cipher, "ping")
        if capability.get("memory_read") is not True:
            raise ApiError(409, 409, "原运行时不支持记忆快照，请先升级")
        snapshot = await call_agent(
            old, cipher, "read-memory", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}
        )
    except (AgentError, TimeoutError) as exc:
        if allow_stored and bot.memory_snapshot_at is not None:
            return "deployed_stored"
        raise ApiError(
            502, 502, "无法回收最新记忆；请恢复原运行时，或明确选择使用上次记忆快照"
        ) from exc
    raw = snapshot.get("memories")
    if not isinstance(raw, list) or len(raw) > 500:
        raise ApiError(502, 502, "原运行时记忆快照无效")
    try:
        entries = [MemoryInput.model_validate(row) for row in raw]
    except ValueError as exc:
        raise ApiError(502, 502, "原运行时记忆快照无效") from exc
    if any(entry.working_dir != bot.working_dir for entry in entries):
        raise ApiError(409, 409, "原运行时返回了其他员工的记忆")
    await collect(session, old, entries, utcnow(), allow_migrating=True)
    bot.memory_snapshot_at = utcnow()
    return "transferred"


async def deploy(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    target_directory: str | None = None,
) -> None:
    memories = list(
        await session.scalars(
            select(Memory).where(Memory.bot_id == bot.id).order_by(Memory.file_name)
        )
    )
    try:
        capability = await call_agent(target, cipher, "ping")
        if capability.get("memory_protocol") != 2:
            raise ApiError(409, 409, "目标运行时不支持安全记忆部署，请先升级")
        payload = {
            "protocol_version": 2,
            "bot_id": str(bot.id),
            "memories": [
                {
                    "working_dir": target_directory or bot.working_dir,
                    "file_name": row.file_name,
                    "content": row.content,
                    "content_hash": row.content_hash,
                    "file_mtime": row.file_mtime.timestamp() if row.file_mtime else None,
                    "deleted": row.deleted_at is not None,
                }
                for row in memories
            ],
        }
        if (
            len(memories) > 500
            or len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BATCH_BYTES - 1024
        ):
            raise ApiError(413, 413, "记忆总量超过目标运行时请求限制")
        if memories:
            result = await call_agent(target, cipher, "deploy-memory", payload)
            if result.get("protocol_version") != 2 or result.get("count") != len(memories):
                raise ApiError(502, 502, "目标运行时未确认完整的记忆部署")
    except (AgentError, TimeoutError, ValueError) as exc:
        raise ApiError(502, 502, "记忆部署未完成，仍保留原运行时绑定") from exc


async def transfer(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    target_directory: str | None = None,
) -> str:
    """兼容独立记忆迁移调用；完整换机由 workspace_transfer 编排。"""
    await ensure_idle(session, bot)
    status = await recover(session, bot, cipher)
    await deploy(session, bot, target, cipher, target_directory=target_directory)
    return status
