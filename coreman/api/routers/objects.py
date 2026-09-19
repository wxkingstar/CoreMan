"""签名附件下载；始终作为附件返回，不能执行模型/用户上传的 HTML。"""

import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.core.config import Settings
from coreman.core.db.models import StoredObject
from coreman.core.object_store import LocalObjectStore
from coreman.core.s3_store import S3ObjectStore
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["objects"])


@router.get("/api/objects/{identity}")
async def download(
    identity: uuid.UUID,
    expires: int,
    signature: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    cfg = request.app.state.settings
    store = LocalObjectStore(cfg.object_storage_root, cfg.master_key_bytes, cfg.public_base_url)
    now = utcnow()
    if not store.valid(identity, expires, signature, now):
        raise ApiError(403, 403, "附件链接无效或已过期")
    row = await session.get(StoredObject, identity)
    if row is None or row.expires_at <= now:
        raise not_found("附件不存在或已过期")
    return await serve(cfg, row)


async def serve(cfg: Settings, row: StoredObject) -> Response:
    """Stream a stored object as a download that browsers never render."""
    store = LocalObjectStore(cfg.object_storage_root, cfg.master_key_bytes, cfg.public_base_url)
    if row.backend == "s3":
        try:
            remote = S3ObjectStore(cfg)
            source = remote.read(row)
            first = await anext(source, b"")
        except Exception:
            raise ApiError(502, 502, "附件存储暂时不可用") from None

        async def content() -> AsyncIterator[bytes]:
            try:
                if first:
                    yield first
                async for part in source:
                    yield part
            finally:
                await source.aclose()

        return StreamingResponse(
            content(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(row.size),
                "Content-Disposition": "attachment; filename*=utf-8''"
                + quote(row.filename, safe=""),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if row.backend != "local":
        raise not_found("附件存储类型不支持")
    path = store.path(row.id)
    if not path.is_file():
        raise not_found("附件文件不存在")
    return FileResponse(
        path,
        filename=row.filename,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
