"""记忆回收：当前实例归属、文件快照与删除墓碑。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, Memory, RelayServer
from coreman.core.errors import ApiError
from coreman.core.timeutils import aware_utc

MAX_FILE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024


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
    session: AsyncSession, relay: RelayServer, entries: list[MemoryInput], now: datetime
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
    await session.flush()
    return result
