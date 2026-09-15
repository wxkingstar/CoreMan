"""换机前同步记忆；实例未改绑之前完成所有可验证步骤。"""

from __future__ import annotations

import asyncio
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


async def transfer(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    target_directory: str | None = None,
) -> str:
    old = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    memories = list(await session.scalars(select(Memory).where(Memory.bot_id == bot.id)))
    if await session.scalar(
        select(Task.id)
        .where(Task.bot_id == bot.id, Task.kind == "skill_install", Task.status.in_(OPEN))
        .limit(1)
    ):
        raise ApiError(409, 409, "技能正在安装，请完成或取消后再换机")
    configured = bool(old and old.agent_port and old.agent_token_enc)
    if not configured and not memories:
        return "not_configured"
    if cipher is None or not target.agent_port or not target.agent_token_enc:
        raise ApiError(409, 409, "有记忆需要迁移，请先配置目标实例 Agent")
    in_flight = await session.scalar(
        select(Task.id)
        .where(
            Task.bot_id == bot.id,
            (
                (Task.status.in_(ACTIVE) & Task.kind.not_in(["relay_switch"]))
                | (Task.kind == "skill_install") & Task.status.in_(OPEN)
            ),
        )
        .limit(1)
    )
    if in_flight is not None:
        raise ApiError(409, 409, "请先等待或停止正在执行的任务，再迁移记忆")
    try:
        async with asyncio.timeout(400):
            capability = await call_agent(target, cipher, "ping")
            if capability.get("memory_protocol") != 2:
                raise ApiError(409, 409, "目标 Agent 不支持安全记忆部署，请先升级")
            if configured and old:
                capability = await call_agent(old, cipher, "ping")
                if capability.get("memory_read") is not True:
                    raise ApiError(409, 409, "原 Agent 不支持换机记忆快照，请先升级")
                snapshot = await call_agent(
                    old, cipher, "read-memory", {"working_dir": bot.working_dir}
                )
                raw = snapshot.get("memories")
                if not isinstance(raw, list) or len(raw) > 500:
                    raise ApiError(502, 502, "原实例记忆快照无效")
                entries = [MemoryInput.model_validate(row) for row in raw]
                if any(entry.working_dir != bot.working_dir for entry in entries):
                    raise ApiError(409, 409, "原实例返回了不属于机器人的记忆目录")
                await collect(session, old, entries, utcnow())
            memories = list(
                await session.scalars(
                    select(Memory).where(Memory.bot_id == bot.id).order_by(Memory.file_name)
                )
            )
            payload = {
                "protocol_version": 2,
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
                raise ApiError(413, 413, "记忆总量超过目标 Agent 请求限制")
            if memories:
                result = await call_agent(target, cipher, "deploy-memory", payload)
                if result.get("protocol_version") != 2 or result.get("count") != len(memories):
                    raise ApiError(502, 502, "目标实例未确认完整的记忆部署")
    except (AgentError, TimeoutError, ValueError) as exc:
        raise ApiError(
            502, 502, "记忆迁移未完成，机器人仍保留原实例；请检查实例和文件状态"
        ) from exc
    return "transferred" if configured else "deployed_stored"
