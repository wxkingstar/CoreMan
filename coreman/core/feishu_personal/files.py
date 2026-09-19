"""Move files between the owner's Feishu data and the agent on the runtime node.

The agent cannot hold the owner's token, so CoreMan does the Feishu side and keeps the bytes in
the object store for a short time:

- Download: a tool fetches the file as the owner and returns a link. The agent downloads it with
  plain HTTP (`curl`), so no credential has to reach its shell.
- Upload: a tool returns a one-time upload link; the agent PUTs the file there and gets an
  upload ID, which the send and save tools accept.

Links and upload IDs are sealed with the master key and name the owner, the bot and the grant
generation. They expire within 30 minutes (links) or an hour (IDs), and stop working as soon as
the owner revokes or reconnects.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuPersonalGrant, StoredObject
from coreman.core.feishu_personal import service
from coreman.core.feishu_personal.policy import Scope
from coreman.core.object_store import LocalObjectStore, object_store
from coreman.core.s3_store import S3ObjectStore
from coreman.core.timeutils import utcnow

if TYPE_CHECKING:
    from coreman.core.config import Settings

LINK_TTL = timedelta(minutes=30)
OBJECT_TTL = timedelta(hours=1)
DOWNLOAD_LIMIT = 50 * 1024 * 1024
UPLOAD_LIMIT = 30 * 1024 * 1024
_AAD = {
    "download": "feishu_personal.file_download.v1",
    "upload": "feishu_personal.file_upload.v1",
    "ref": "feishu_personal.file_ref.v1",
}
DOWNLOAD_PATH = "/api/runtime/feishu-personal/files/"
UPLOAD_PATH = "/api/runtime/feishu-personal/uploads/"


@dataclass(frozen=True)
class Claims:
    bot_id: uuid.UUID
    user_id: uuid.UUID
    epoch: uuid.UUID
    expires: float
    extra: dict[str, Any]


def content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


@dataclass(frozen=True)
class FileRelay:
    settings: Settings
    cipher: Cipher

    def store(self, backend: str | None = None) -> LocalObjectStore:
        return object_store(self.settings, backend)

    def _base(self) -> str:
        return self.settings.public_base_url.rstrip("/")

    def seal(self, kind: str, grant: FeishuPersonalGrant, ttl: timedelta, **extra: Any) -> str:
        claims = {
            "b": str(grant.bot_id),
            "u": str(grant.user_id),
            "e": str(grant.context_epoch),
            "x": (utcnow() + ttl).timestamp(),
            **extra,
        }
        raw = self.cipher.seal(json.dumps(claims, separators=(",", ":")), _AAD[kind])
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def open(self, kind: str, token: str) -> Claims:
        """Claims of a sealed value; ValueError when forged, damaged or expired."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            data = json.loads(self.cipher.open(raw, _AAD[kind]))
            claims = Claims(
                uuid.UUID(data.pop("b")),
                uuid.UUID(data.pop("u")),
                uuid.UUID(data.pop("e")),
                float(data.pop("x")),
                data,
            )
        except (binascii.Error, ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid_file_token") from exc
        if claims.expires <= utcnow().timestamp():
            raise ValueError("expired_file_token")
        return claims

    async def save(
        self,
        session: AsyncSession,
        source: AsyncIterator[bytes],
        *,
        filename: str,
        mime: str,
    ) -> StoredObject:
        row = await self.store().put(
            session, source, filename=filename, content_type=mime, now=utcnow()
        )
        # Personal data: keep it only as long as the agent needs to fetch or send it.
        row.expires_at = utcnow() + OBJECT_TTL
        await session.flush()
        return row

    def download_link(self, grant: FeishuPersonalGrant, row: StoredObject) -> str:
        token = self.seal("download", grant, LINK_TTL, o=str(row.id))
        return self._base() + DOWNLOAD_PATH + token

    def upload_link(self, grant: FeishuPersonalGrant, filename: str, mime: str) -> str:
        token = self.seal("upload", grant, LINK_TTL, n=filename, c=mime)
        return self._base() + UPLOAD_PATH + token

    def upload_id(self, grant: FeishuPersonalGrant, row: StoredObject) -> str:
        return self.seal("ref", grant, OBJECT_TTL, o=str(row.id))

    async def uploaded(self, session: AsyncSession, scope: Scope, upload_id: str) -> StoredObject:
        """The file behind an upload ID, if it belongs to this owner's current grant."""
        try:
            claims = self.open("ref", upload_id)
            identity = uuid.UUID(str(claims.extra["o"]))
        except (ValueError, KeyError) as exc:
            raise service.PersonalError("upload_not_found") from exc
        grant = await service.existing_row(session, self.cipher, scope)
        if not current(grant, claims):
            raise service.PersonalError("upload_not_found")
        row = await session.get(StoredObject, identity)
        if row is None or row.expires_at <= utcnow():
            raise service.PersonalError("upload_not_found")
        return row

    async def read(self, row: StoredObject) -> bytes:
        chunks = bytearray()
        store = self.store(row.backend)
        if isinstance(store, S3ObjectStore):
            async for chunk in store.read(row):
                chunks.extend(chunk)
        else:
            chunks.extend(await asyncio.to_thread(store.path(row.id).read_bytes))
        return bytes(chunks)


def current(grant: FeishuPersonalGrant | None, claims: Claims) -> bool:
    """The sealed owner still holds the same, connected grant."""
    return bool(
        grant is not None
        and grant.bot_id == claims.bot_id
        and grant.user_id == claims.user_id
        and grant.context_epoch == claims.epoch
        and grant.status == "connected"
        and grant.token_enc
    )
