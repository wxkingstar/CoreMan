"""本地对象存储：随机文件键、原子落盘、24 小时签名下载。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from coreman.core.config import Settings

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import StoredObject

MAX_SIZE = 100 * 1024 * 1024
LINK_TTL = 86400


async def disk_io[T](operation: Callable[[], T]) -> T:
    """取消时等待已开始的文件操作结束，避免后台线程继续使用已关闭/重用的 fd。"""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class LocalObjectStore:
    def __init__(self, root: str, signing_key: bytes, public_base_url: str):
        self.root = Path(root).expanduser().resolve() / "objects"
        self.key = hmac.digest(signing_key, b"coreman-object-link", "sha256")
        self.public_base_url = public_base_url.rstrip("/")

    def path(self, identity: uuid.UUID) -> Path:
        path = self.root / identity.hex
        # 本机/卷被错误写入符号链接时也不跟随链接离开附件目录。
        if path.is_symlink():
            raise ValueError("invalid object path")
        return path

    def token(self, identity: uuid.UUID, expires: int) -> str:
        return hmac.new(self.key, f"{identity}:{expires}".encode(), hashlib.sha256).hexdigest()

    def url(self, row: StoredObject) -> str:
        expires = int(row.expires_at.timestamp())
        params = urlencode({"expires": expires, "signature": self.token(row.id, expires)})
        return f"{self.public_base_url}/api/objects/{row.id}?{params}"

    def valid(self, identity: uuid.UUID, expires: int, signature: str, now: datetime) -> bool:
        return (
            0 < expires - now.timestamp() <= LINK_TTL + 60
            and len(signature) == 64
            and hmac.compare_digest(self.token(identity, expires).encode(), signature.encode())
        )

    async def put(
        self,
        session: AsyncSession,
        source: AsyncIterator[bytes],
        *,
        filename: str,
        content_type: str,
        now: datetime,
    ) -> StoredObject:
        if self.root.is_symlink():
            raise ValueError("invalid object directory")
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True, mode=0o700)
        identity = uuid.uuid4()
        size = 0
        digest = hashlib.sha256()
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in source:
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise ValueError("object size limit")
                    await disk_io(partial(output.write, chunk))
                    digest.update(chunk)
                await disk_io(output.flush)
                await disk_io(lambda: os.fsync(output.fileno()))
            os.replace(temporary, self.path(identity))
        finally:
            await asyncio.to_thread(Path(temporary).unlink, missing_ok=True)
        safe_name = (
            Path(filename.replace("\\", "/"))
            .name.replace("\r", "")
            .replace("\n", "")
            .replace("\x00", "")[:200]
            or "attachment"
        )
        row = StoredObject(
            id=identity,
            backend="local",
            filename=safe_name,
            content_type=content_type[:200],
            size=size,
            sha256=digest.hexdigest(),
            expires_at=now + timedelta(seconds=LINK_TTL),
        )
        session.add(row)
        await session.flush()
        return row

    async def cleanup(self, session: AsyncSession, now: datetime) -> int:
        """每轮最多 500 条元数据及 500 个旧孤儿；删除可重复，失败保留元数据重试。"""
        rows = list(
            await session.scalars(
                select(StoredObject)
                .where(StoredObject.backend == "local", StoredObject.expires_at <= now)
                .order_by(StoredObject.expires_at)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        )
        count = 0
        for row in rows:
            path = self.root / row.id.hex
            # unlink 删除链接本身，不跟随；存储根目录异常则整轮拒绝。
            if self.root.is_symlink():
                raise ValueError("invalid object directory")
            await disk_io(partial(path.unlink, missing_ok=True))
            await session.delete(row)
            count += 1
        await session.flush()
        # 文件已经落盘、数据库提交之前崩溃可能留下孤儿。超过 25 小时才回收，
        # 远长于 180 秒上传任务预算；不触碰新文件或其它文件名。
        cutoff = now.timestamp() - LINK_TTL - 3600

        def candidates() -> list[Path]:
            if self.root.is_symlink():
                raise ValueError("invalid object directory")
            if not self.root.exists():
                return []
            found = []
            with os.scandir(self.root) as entries:
                for entry in entries:
                    name = entry.name
                    if not (
                        name.startswith(".upload-")
                        or (len(name) == 32 and all(c in "0123456789abcdef" for c in name))
                    ):
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            found.append(Path(entry.path))
                    except FileNotFoundError:
                        continue
                    if len(found) >= 500:
                        break
            return found

        for path in await disk_io(candidates):
            if (
                not path.name.startswith(".upload-")
                and await session.get(StoredObject, uuid.UUID(hex=path.name)) is not None
            ):
                continue
            await disk_io(partial(path.unlink, missing_ok=True))
            count += 1
        return count


def object_store(cfg: Settings, backend: str | None = None) -> LocalObjectStore:
    if (backend or cfg.object_storage) == "s3":
        from coreman.core.s3_store import S3ObjectStore

        return S3ObjectStore(cfg)
    return LocalObjectStore(cfg.object_storage_root, cfg.master_key_bytes, cfg.public_base_url)
