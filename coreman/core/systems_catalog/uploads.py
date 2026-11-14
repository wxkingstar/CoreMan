"""Files for multipart operations (uploads, imports) of platform-proxied systems.

The runtime holds no business token, so it cannot send a file to the system itself. It PUTs the
file to CoreMan with this turn's catalog capability and gets an upload ID; `systems_call` takes
`{"upload_id": ...}` in place of each file field, and the platform sends the stored bytes as the
member. An upload belongs to the turn and member that uploaded it and expires after an hour, which
outlives the confirmation card of a write.
"""

from __future__ import annotations

import asyncio
import mimetypes
import shlex
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import StoredObject
from coreman.core.object_store import object_store
from coreman.core.systems_catalog import policy
from coreman.core.timeutils import utcnow

if TYPE_CHECKING:
    from coreman.core.config import Settings
    from coreman.core.systems_catalog.context import Context

UPLOAD_PATH = "/api/runtime/systems/uploads"
UPLOAD_LIMIT = 30 * 1024 * 1024
TTL = timedelta(hours=1)
META_KEY = "systems_upload"


def content_type(filename: str, declared: str | None) -> str:
    base = (declared or "").split(";", 1)[0].strip().lower()
    # curl --data-binary 不带 -H 时声明的是表单编码，这种和 octet-stream 一样按文件名判断。
    if "/" in base and base not in (
        "application/octet-stream",
        "application/x-www-form-urlencoded",
    ):
        return base
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


async def save(
    session: AsyncSession,
    settings: Settings,
    source: AsyncIterator[bytes],
    *,
    filename: str,
    mime: str,
    task_id: int,
    user_id: uuid.UUID,
) -> StoredObject:
    now = utcnow()
    row = await object_store(settings).put(
        session, source, filename=filename, content_type=mime, now=now
    )
    row.expires_at = now + TTL
    row.storage_metadata = {
        **row.storage_metadata,
        META_KEY: {"task_id": task_id, "user_id": str(user_id)},
    }
    await session.flush()
    return row


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"upload_id"}


async def resolve(ctx: Context, upload_id: Any) -> StoredObject | None:
    """The upload this turn's member made in this turn, unless it expired."""
    try:
        identity = uuid.UUID(str(upload_id))
    except ValueError:
        return None
    row = await ctx.session.get(StoredObject, identity)
    owner = (row.storage_metadata or {}).get(META_KEY) if row is not None else None
    if (
        row is None
        or not isinstance(owner, dict)
        or owner.get("task_id") != ctx.member.task_id
        or owner.get("user_id") != str(ctx.member.user.id)
        or row.expires_at <= utcnow()
    ):
        return None
    return row


async def read(settings: Settings, row: StoredObject) -> bytes:
    from coreman.core.s3_store import S3ObjectStore

    store = object_store(settings, row.backend)
    chunks = bytearray()
    if isinstance(store, S3ObjectStore):
        async for chunk in store.read(row):
            chunks.extend(chunk)
    else:
        chunks.extend(await asyncio.to_thread(store.path(row.id).read_bytes))
    return bytes(chunks)


def describe(row: StoredObject) -> dict[str, Any]:
    return {
        "filename": row.filename,
        "content_type": row.content_type,
        "bytes": row.size,
        "sha256": row.sha256,
    }


def instructions(public_base_url: str, files: dict[str, dict[str, bool]]) -> dict[str, Any]:
    """How the runtime uploads the files of a multipart operation (part of systems_describe)."""
    url = public_base_url.rstrip("/") + UPLOAD_PATH + "?filename=<文件名>"
    curl = (
        f'curl -sS -X PUT -H "Authorization: Bearer ${policy.TOKEN_ENV}" '
        f"--data-binary @<本地文件路径> {shlex.quote(url)}"
    )
    fields = ", ".join(
        f'"{name}": ' + ('[{"upload_id": "..."}]' if spec["many"] else '{"upload_id": "..."}')
        for name, spec in files.items()
    )
    return {
        "file_fields": sorted(files),
        "curl": curl,
        "body": "{" + fields + (", 其他字段..." if fields else "") + "}",
        "notes": "这个操作要上传文件：先在运行环境里用 curl 把文件 PUT 到平台，拿到返回的 "
        'upload_id；调用 systems_call 时文件字段传 {"upload_id": ...}，其他字段照常传。'
        f"单个文件不超过 {UPLOAD_LIMIT // (1024 * 1024)} MB，1 小时内、只在本轮有效。",
    }
