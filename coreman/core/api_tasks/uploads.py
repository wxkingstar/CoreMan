"""业务系统代员工发起任务时附带的图片：先逐张上传，任务里按 upload_id 引用。

一次一张、单张不超过 10MB，按文件头认 JPEG / PNG / WebP。上传属于「调用方 + 员工」，别人的
任务引用不到。没被任务引用的 24 小时后清掉；被引用的跟着任务保留 30 天，引用回复时还能再看。
前 `INLINE` 张直接放进这一轮给模型看，更多的给 AI 员工限时下载链接，让它分批下载、逐张打开。
见 docs/superpowers/specs/2026-11-24-api-tasks-system-clients.md 第 5.1、6.2 节。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.api_tasks.tokens import TokenError
from coreman.core.crypto import Cipher
from coreman.core.db.models import ApiTaskUpload
from coreman.core.timeutils import utcnow
from coreman.core.wecom.media import Media, MediaError

MAX_BYTES = 10 * 1024 * 1024
UNUSED_TTL = timedelta(hours=24)
KEEP = timedelta(days=30)
# 这一轮直接放进模型输入的张数；更多的给下载链接，由 AI 员工分批下载打开。
INLINE = 8
LINK_TTL = timedelta(hours=24)
LINK_AAD = "api_task_upload_link.v1"
SWEEP_BATCH = 200
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff(data: bytes) -> str | None:
    """按文件头认格式，不信调用方给的文件名和类型。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def save(
    session: AsyncSession, *, client_key: str, user_id: uuid.UUID, data: bytes
) -> ApiTaskUpload:
    """存一张图；同一调用方同一员工传过同样的内容就返回原来那张。调用方负责提交。"""
    if not data:
        raise TokenError(422, "文件是空的")
    if len(data) > MAX_BYTES:
        raise TokenError(413, f"单张图片不能超过 {MAX_BYTES // 1024 // 1024}MB")
    content_type = sniff(data)
    if content_type is None:
        raise TokenError(415, "只支持 JPEG、PNG、WebP 图片")
    digest = hashlib.sha256(data).hexdigest()
    now = utcnow()
    stmt = (
        insert(ApiTaskUpload)
        .values(
            id=uuid.uuid4(),
            client_key=client_key,
            user_id=user_id,
            sha256=digest,
            size=len(data),
            content_type=content_type,
            data=data,
            expires_at=now + UNUSED_TTL,
        )
        .on_conflict_do_nothing(constraint="uq_api_task_uploads_owner_sha256")
    )
    await session.execute(stmt)
    row = await session.scalar(
        select(ApiTaskUpload)
        .where(
            ApiTaskUpload.client_key == client_key,
            ApiTaskUpload.user_id == user_id,
            ApiTaskUpload.sha256 == digest,
        )
        .execution_options(populate_existing=True)
    )
    assert row is not None
    # 重传一张还没被用上的：重新算 24 小时，免得刚传上就被清掉。
    row.expires_at = max(row.expires_at, now + UNUSED_TTL)
    await session.flush()
    return row


async def claim(
    session: AsyncSession, *, client_key: str, user_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """任务要引用的图片：都得是这个调用方为这位员工传的、还没过期；被引用后保留 30 天。"""
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    now = utcnow()
    rows = {
        row.id: row
        for row in await session.scalars(
            select(ApiTaskUpload)
            .where(ApiTaskUpload.id.in_(wanted))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    for identity in wanted:
        row = rows.get(identity)
        if (
            row is None
            or row.client_key != client_key
            or row.user_id != user_id
            or row.expires_at <= now
        ):
            raise TokenError(404, f"图片 {identity} 不存在或已过期，请重新上传")
    await session.execute(
        update(ApiTaskUpload)
        .where(ApiTaskUpload.id.in_(wanted), ApiTaskUpload.expires_at < now + KEEP)
        .values(expires_at=now + KEEP)
    )
    return wanted


def _filename(row: ApiTaskUpload, index: int) -> str:
    return f"image-{index:03d}.{EXTENSIONS.get(row.content_type, 'img')}"


def link(cipher: Cipher, base_url: str, upload_id: uuid.UUID, now: datetime) -> str:
    """限时下载链接：令牌是平台密钥加密的「图片 + 到期时间」，改不了也伪造不了。"""
    expires = int((now + LINK_TTL).timestamp())
    token = cipher.encrypt(f"{upload_id}:{expires}", LINK_AAD)
    query = urlencode({"token": token})
    return f"{base_url.rstrip('/')}/api/infra/tasks/uploads/{upload_id}/raw?{query}"


def link_valid(cipher: Cipher, upload_id: uuid.UUID, token: str, now: datetime) -> bool:
    try:
        plain = cipher.decrypt(token, LINK_AAD)
    except ValueError:
        return False
    identity, _, raw = plain.partition(":")
    if identity != str(upload_id) or not raw.isdecimal():
        return False
    return now.timestamp() < int(raw)


def turn_parts(
    prompt: str, image_ids: Sequence[uuid.UUID], *, api_task_id: uuid.UUID
) -> list[dict[str, Any]]:
    """合成入站事件的 parts：任务内容加每张图（ref 指向上传记录，由 `UploadFetcher` 取）。"""
    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    parts += [
        {"type": "image", "ref": {"message_id": f"api:{api_task_id}", "file_key": str(identity)}}
        for identity in image_ids
    ]
    return parts


def split_for_turn(
    parts: list[dict[str, Any]], *, cipher: Cipher, base_url: str, now: datetime
) -> list[dict[str, Any]]:
    """开轮时：前 `INLINE` 张留给模型直接看，其余换成一段下载清单（链接这时才签，24 小时有效）。"""
    images = [p for p in parts if p.get("type") == "image"]
    if len(images) <= INLINE:
        return parts
    rest = images[INLINE:]
    lines = [
        f"本次共 {len(images)} 张图片：前 {INLINE} 张已直接附在这条消息里；"
        f"其余 {len(rest)} 张请用下面的链接下载到工作目录 api-inputs/ 下（链接 24 小时内有效），"
        "分批逐张打开查看，全部看完再给结论，不要跳过："
    ]
    for index, part in enumerate(rest, INLINE + 1):
        identity = uuid.UUID(str(part["ref"]["file_key"]))
        lines.append(f"{index}. {link(cipher, base_url, identity, now)}")
    kept = [p for p in parts if p.get("type") != "image"] + images[:INLINE]
    return [*kept, {"type": "text", "text": "\n".join(lines)}]


class UploadFetcher:
    """`ContentBuilder` 的下载器：API 任务里的图片从上传记录里取，不访问任何外部地址。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], api_task_id: str) -> None:
        self.session_factory = session_factory
        self.message_id = f"api:{api_task_id}"

    async def fetch_image(self, ref: dict[str, Any]) -> Media:
        if ref.get("message_id") != self.message_id:
            raise MediaError("download_failed")
        try:
            identity = uuid.UUID(str(ref.get("file_key")))
        except ValueError:
            raise MediaError("download_failed") from None
        async with self.session_factory() as session:
            row = await session.get(ApiTaskUpload, identity)
            if row is None:
                raise MediaError("resource_expired")
            return Media(bytes(row.data), row.content_type, _filename(row, 1))

    async def fetch_file(self, ref: dict[str, Any], *, filename: str | None) -> Media:
        raise MediaError("download_failed")

    async def aclose(self) -> None:
        return None


async def sweep(session: AsyncSession, now: datetime) -> int:
    """到期的上传连同图片内容删掉；任务行里的引用留着，查看时按「已过期」处理。"""
    ids = list(
        await session.scalars(
            select(ApiTaskUpload.id)
            .where(ApiTaskUpload.expires_at <= now)
            .order_by(ApiTaskUpload.expires_at)
            .limit(SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    if ids:
        await session.execute(delete(ApiTaskUpload).where(ApiTaskUpload.id.in_(ids)))
    return len(ids)
