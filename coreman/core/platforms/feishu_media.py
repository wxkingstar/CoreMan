"""飞书消息资源下载只使用 message_id/file_key，拒绝任意 URL 与跨消息引用。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.wecom.media import (
    FILE_TIMEOUT,
    IMAGE_TIMEOUT,
    MAX_BYTES,
    Media,
    MediaError,
    MediaFetcher,
    guess_filename,
    mime_for,
    sniff_image_mime,
)

_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class FeishuMediaFetcher(MediaFetcher):
    def __init__(self, client: FeishuClient, *, message_id: str, max_bytes: int = MAX_BYTES):
        self.client = client
        self.message_id = message_id
        self.max_bytes = max_bytes

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _resource(
        self, ref: dict[str, Any], kind: str, budget: float
    ) -> tuple[bytes, httpx.Headers]:
        mid, key = str(ref.get("message_id") or ""), str(ref.get("file_key") or "")
        if mid != self.message_id or not _ID.fullmatch(mid) or not _ID.fullmatch(key):
            raise MediaError("download_failed", "invalid message resource")
        try:
            async with asyncio.timeout(budget):
                token = await self.client.get_token()
                async with self.client._http.stream(
                    "GET",
                    f"/open-apis/im/v1/messages/{mid}/resources/{key}",
                    params={"type": kind},
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code != 200:
                        raise MediaError("download_failed", "platform rejected resource")
                    declared = response.headers.get("content-length", "")
                    if declared.isdecimal() and int(declared) > self.max_bytes:
                        raise MediaError("too_large")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > self.max_bytes:
                            raise MediaError("too_large")
                    return bytes(chunks), response.headers
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise MediaError("timeout") from exc
        except (httpx.HTTPError, FeishuError) as exc:
            raise MediaError("download_failed") from exc

    async def fetch_image(self, ref: dict[str, Any]) -> Media:
        data, _ = await self._resource(ref, "image", IMAGE_TIMEOUT)
        return Media(data, sniff_image_mime(data), None)

    async def fetch_file(self, ref: dict[str, Any], *, filename: str | None) -> Media:
        data, headers = await self._resource(ref, "file", FILE_TIMEOUT)
        name = guess_filename(
            given=filename, content_disposition=headers.get("content-disposition"), data=data
        )
        return Media(data, mime_for(name), name)
