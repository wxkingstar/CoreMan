"""记忆回收：当前实例归属、文件快照与删除墓碑。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, Memory, RelayServer, RuntimeNode
from coreman.core.errors import ApiError
from coreman.core.timeutils import aware_utc

MAX_FILE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
# 节点把员工记忆放在工作目录的这个子目录里，Claude 与 Codex 共用。
MEMORY_DIR = ".coreman-memory"
RUNTIME_CAPABILITY = "memory_layout_v1"


def file_name(value: str) -> str:
    if (
        not value.endswith(".md")
        or len(value) > 200
        or PurePosixPath(value).name != value
        or any(char in value for char in ("\\", "\x00", "\r", "\n"))
    ):
        raise ValueError("记忆文件须为不含路径的 Markdown 文件名")
    return value


def working_dir(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("记忆工作目录必须是无上级跳转的绝对路径")
    return str(path)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    working_dir: str = Field(min_length=1, max_length=500)
    # 已废弃：记忆不再放在 Claude 的项目目录里。旧 Agent 仍会带上，收下不用。
    project_dir_name: str | None = Field(default=None, max_length=1000)
    file_name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=MAX_FILE_BYTES)
    content_hash: str | None = Field(default=None, max_length=64)
    file_mtime: datetime

    @field_validator("working_dir")
    @classmethod
    def directory(cls, value: str) -> str:
        return working_dir(value)

    @field_validator("file_name")
    @classmethod
    def name(cls, value: str) -> str:
        return file_name(value)

    @field_validator("content")
    @classmethod
    def size(cls, value: str) -> str:
        if len(value.encode()) > MAX_FILE_BYTES:
            raise ValueError("记忆文件超过 256 KiB")
        return value

    @field_validator("file_mtime")
    @classmethod
    def timezone(cls, value: datetime) -> datetime:
        return aware_utc(value)


async def collect(
    session: AsyncSession,
    relay: RelayServer,
    entries: list[MemoryInput],
    now: datetime,
    *,
    allow_migrating: bool = False,
) -> dict[str, int]:
    if len(entries) > 500 or sum(len(row.content.encode()) for row in entries) > MAX_BATCH_BYTES:
        raise ApiError(413, 413, "记忆批次超过限制")
    if any(row.file_mtime > now + timedelta(seconds=300) for row in entries):
        raise ApiError(422, 422, "记忆时间超前，请检查实例时钟")
    # 按 bot 工作目录固定顺序加锁，串行处理同一文件的首次插入和管理台编辑。
    directories = sorted({row.working_dir for row in entries})
    bots = list(
        await session.scalars(
            select(Bot)
            .where(Bot.relay_server_id == relay.id, Bot.working_dir.in_(directories))
            .order_by(Bot.id)
            .with_for_update()
        )
    )
    if not allow_migrating and any(bot.workspace_state == "migrating" for bot in bots):
        raise ApiError(409, 409, "员工正在迁移，暂缓后台记忆回收")
    by_directory: dict[str, list[Bot]] = {}
    for bot in bots:
        by_directory.setdefault(working_dir(bot.working_dir), []).append(bot)
    if any(len(by_directory.get(directory, [])) != 1 for directory in directories):
        raise ApiError(409, 409, "记忆目录未唯一分配给本实例机器人，整个批次未写入")
    result = {"created": 0, "updated": 0, "unchanged": 0, "stale": 0, "deleted": 0}
    for entry in entries:
        bot = by_directory[entry.working_dir][0]
        digest = content_hash(entry.content)
        if entry.content_hash is not None and entry.content_hash != digest:
            raise ApiError(422, 422, "记忆内容与校验值不一致")
        row = await session.scalar(
            select(Memory)
            .where(Memory.bot_id == bot.id, Memory.file_name == entry.file_name)
            .with_for_update()
        )
        if row is None:
            count = await session.scalar(
                select(func.count()).select_from(Memory).where(Memory.bot_id == bot.id)
            )
            if count is not None and count >= 500:
                raise ApiError(409, 409, "机器人记忆文件已达上限，整个批次未写入")
            session.add(
                Memory(
                    bot_id=bot.id,
                    file_name=entry.file_name,
                    content=entry.content,
                    content_hash=digest,
                    collected_from=str(relay.id),
                    file_mtime=entry.file_mtime,
                )
            )
            await session.flush()
            result["created"] += 1
        elif row.deleted_at is not None:
            result["deleted"] += 1
        elif row.content_hash == digest:
            if row.file_mtime is None or row.file_mtime < entry.file_mtime:
                row.file_mtime, row.updated_at = entry.file_mtime, now
            result["unchanged"] += 1
        elif row.file_mtime and row.file_mtime >= entry.file_mtime:
            result["stale"] += 1
        else:
            row.content, row.content_hash = entry.content, digest
            row.file_mtime, row.collected_from, row.updated_at = (
                entry.file_mtime,
                str(relay.id),
                now,
            )
            result["updated"] += 1
    for bot in bots:
        bot.memory_snapshot_at = now
    await session.flush()
    return result


async def prompt_dir(
    session: AsyncSession, relay: RelayServer | None, backend: str, directory: str
) -> str:
    """要写进 Codex 提示的记忆目录；节点还不支持共用记忆时为空，不提。"""
    if backend != "codex" or relay is None or relay.runtime_node_id is None:
        return ""
    node = await session.get(RuntimeNode, relay.runtime_node_id, populate_existing=True)
    capability = ((node.capabilities if node else None) or {}).get(backend) or {}
    if not isinstance(capability, dict) or capability.get(RUNTIME_CAPABILITY) is not True:
        return ""
    return str(PurePosixPath(directory) / MEMORY_DIR)
