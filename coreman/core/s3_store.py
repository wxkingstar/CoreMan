"""S3 私有对象存储，签名入口保持 CoreMan URL，不把存储凭据交给浏览器。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import closing
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.config import Settings
from coreman.core.db.models import StoredObject
from coreman.core.object_store import LINK_TTL, MAX_SIZE, LocalObjectStore, disk_io
from coreman.core.relay.safe_transport import validate_host


class S3ObjectStore(LocalObjectStore):
    def __init__(self, cfg: Settings):
        super().__init__(cfg.object_storage_root, cfg.master_key_bytes, cfg.public_base_url)
        if not cfg.s3_bucket or not cfg.s3_access_key or not cfg.s3_secret_key:
            raise ValueError("S3 bucket and explicit credentials are required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", cfg.s3_bucket):
            raise ValueError("invalid S3 bucket")
        if not re.fullmatch(r"[A-Za-z0-9/_-]{1,100}", cfg.s3_prefix) or not re.fullmatch(
            r"[a-z0-9-]{1,50}", cfg.s3_region
        ):
            raise ValueError("invalid S3 prefix or region")
        if cfg.s3_endpoint:
            endpoint = urlsplit(cfg.s3_endpoint)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in ("", "/")
            ):
                raise ValueError("S3 endpoint must be HTTPS without credentials or path")
            validate_host(endpoint.hostname)
        if not cfg.s3_prefix.strip("/"):
            raise ValueError("S3 prefix must not be empty")
        self.cfg = cfg
        self.bucket = cfg.s3_bucket
        self.prefix = cfg.s3_prefix.strip("/") + "/"
        self.store_id = hashlib.sha256(
            json.dumps([cfg.s3_endpoint, cfg.s3_region, cfg.s3_bucket, self.prefix]).encode()
        ).hexdigest()
        self._cursor: str | None = None

    def client(self) -> Any:
        # 使用独立 SDK session，显式凭据，不尝试机器元数据凭据链；不继承代理。
        assert self.cfg.s3_access_key and self.cfg.s3_secret_key
        return boto3.session.Session().client(
            "s3",
            endpoint_url=self.cfg.s3_endpoint,
            region_name=self.cfg.s3_region,
            aws_access_key_id=self.cfg.s3_access_key.get_secret_value(),
            aws_secret_access_key=self.cfg.s3_secret_key.get_secret_value(),
            aws_session_token=self.cfg.s3_session_token.get_secret_value()
            if self.cfg.s3_session_token
            else None,
            config=Config(
                signature_version="s3v4",
                ignore_configured_endpoint_urls=True,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=5,
                read_timeout=30,
                retries={"total_max_attempts": 2, "mode": "standard"},
                proxies={},
                s3={"addressing_style": "path"},
            ),
        )

    def object_key(self, row: StoredObject) -> str:
        if row.backend != "s3" or row.storage_metadata.get("store_id") != self.store_id:
            raise ValueError("object storage configuration changed")
        return self.prefix + row.id.hex

    async def put(
        self,
        session: AsyncSession,
        source: AsyncIterator[bytes],
        *,
        filename: str,
        content_type: str,
        now: datetime,
    ) -> StoredObject:
        identity = uuid.uuid4()
        digest = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        size = 0
        with tempfile.TemporaryFile(mode="w+b") as output:
            async for chunk in source:
                size += len(chunk)
                if size > MAX_SIZE:
                    raise ValueError("object size limit")
                await disk_io(partial(output.write, chunk))
                digest.update(chunk)
                md5.update(chunk)
            await disk_io(partial(output.seek, 0))

            def upload() -> dict[str, Any]:
                with closing(self.client()) as client:
                    result: dict[str, Any] = client.put_object(
                        Bucket=self.bucket,
                        Key=self.prefix + identity.hex,
                        Body=output,
                        ContentLength=size,
                        ContentMD5=base64.b64encode(md5.digest()).decode(),
                        ContentType="application/octet-stream",
                        Metadata={"coreman_expires_at": str(int(now.timestamp()) + LINK_TTL)},
                    )
                    return result

            result = await disk_io(upload)
        safe_name = (
            Path(filename.replace("\\", "/"))
            .name.replace("\r", "")
            .replace("\n", "")
            .replace("\x00", "")[:200]
            or "attachment"
        )
        row = StoredObject(
            id=identity,
            backend="s3",
            filename=safe_name,
            content_type=content_type[:200],
            size=size,
            sha256=digest.hexdigest(),
            expires_at=now + timedelta(seconds=LINK_TTL),
            storage_metadata={"store_id": self.store_id, "version_id": result.get("VersionId")},
        )
        session.add(row)
        await session.flush()
        return row

    async def read(self, row: StoredObject) -> AsyncGenerator[bytes, None]:
        key = self.object_key(row)
        client = self.client()
        body = None
        try:
            args = {"Bucket": self.bucket, "Key": key}
            if row.storage_metadata.get("version_id"):
                args["VersionId"] = row.storage_metadata["version_id"]
            result = await disk_io(partial(client.get_object, **args))
            body = result["Body"]
            if result.get("ContentLength") != row.size or row.size > MAX_SIZE:
                raise ValueError("object size mismatch")
            count = 0
            while part := await disk_io(partial(body.read, 65536)):
                count += len(part)
                if count > row.size:
                    raise ValueError("object size mismatch")
                yield part
            if count != row.size:
                raise ValueError("incomplete object")
        finally:
            if body is not None:
                await disk_io(body.close)
            await disk_io(client.close)

    async def cleanup(self, session: AsyncSession, now: datetime) -> int:
        rows = list(
            await session.scalars(
                select(StoredObject)
                .where(
                    StoredObject.backend == "s3",
                    StoredObject.storage_metadata["store_id"].astext == self.store_id,
                    StoredObject.expires_at <= now,
                )
                .order_by(StoredObject.expires_at)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        )
        objects = [
            {
                "Key": self.object_key(row),
                **(
                    {"VersionId": row.storage_metadata["version_id"]}
                    if row.storage_metadata.get("version_id")
                    else {}
                ),
            }
            for row in rows
        ]
        count = 0
        if objects:

            def remove() -> dict[str, Any]:
                with closing(self.client()) as client:
                    result: dict[str, Any] = client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": objects, "Quiet": False}
                    )
                    return result

            result = await disk_io(remove)
            if result.get("Errors"):
                raise ValueError("S3 object cleanup partially failed")
            deleted = {item["Key"] for item in result.get("Deleted", [])}
            for row in rows:
                if self.object_key(row) in deleted:
                    await session.delete(row)
                    count += 1
            await session.flush()

        # 一轮仅扫一页；游标跨轮推进，避免长期只检查前 1000 个对象。
        def listing() -> dict[str, Any]:
            with closing(self.client()) as client:
                result: dict[str, Any] = client.list_objects_v2(
                    Bucket=self.bucket,
                    Prefix=self.prefix,
                    MaxKeys=1000,
                    **({"ContinuationToken": self._cursor} if self._cursor else {}),
                )
                return result

        page = await disk_io(listing)
        self._cursor = page.get("NextContinuationToken")
        cutoff = now - timedelta(seconds=LINK_TTL + 3600)
        candidates: dict[uuid.UUID, str] = {}
        for item in page.get("Contents", []):
            key = item["Key"]
            suffix = key.removeprefix(self.prefix)
            if (
                not key.startswith(self.prefix)
                or not re.fullmatch(r"[a-f0-9]{32}", suffix)
                or item["LastModified"] >= cutoff
            ):
                continue
            candidates[uuid.UUID(hex=suffix)] = key
        existing = (
            set(
                await session.scalars(
                    select(StoredObject.id).where(StoredObject.id.in_(candidates))
                )
            )
            if candidates
            else set()
        )
        orphan_keys = [
            {"Key": key} for identity, key in candidates.items() if identity not in existing
        ]
        if orphan_keys:

            def remove_orphans() -> dict[str, Any]:
                with closing(self.client()) as client:
                    result: dict[str, Any] = client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": orphan_keys, "Quiet": False}
                    )
                    return result

            removed = await disk_io(remove_orphans)
            if removed.get("Errors"):
                raise ValueError("S3 orphan cleanup partially failed")
            count += len(removed.get("Deleted", []))
        return count
