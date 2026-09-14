"""假的企微媒体下载点：`httpx.MockTransport` 按 aeskey 加密返回字节，可注入状态码/延迟/超限。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from coreman.core.wecom.media import derive_key

BASE = "https://media.test"


def encrypt_media(plain: bytes, aeskey: str) -> bytes:
    """AES-256-CBC + PKCS#7（填充到 32 字节的倍数），与企微长连接文档一致。"""
    key = derive_key(aeskey)
    pad = 32 - len(plain) % 32
    padded = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


@dataclass
class _Entry:
    body: bytes
    status: int
    headers: dict[str, str]
    delay: float


class FakeMedia:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self.hits: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def add(
        self,
        path: str,
        plain: bytes,
        *,
        aeskey: str,
        content_disposition: str | None = None,
        status: int = 200,
        delay: float = 0.0,
        content_length: int | None = None,
        truncate_to: int | None = None,
    ) -> str:
        body = encrypt_media(plain, aeskey)
        if truncate_to is not None:
            body = body[:truncate_to]
        headers: dict[str, str] = {"content-type": "application/octet-stream"}
        if content_disposition:
            headers["content-disposition"] = content_disposition
        if content_length is not None:
            headers["content-length"] = str(content_length)
        self._entries[path] = _Entry(body, status, headers, delay)
        return BASE + path

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits.append(path)
        entry = self._entries.get(path)
        if entry is None:
            return httpx.Response(404)
        if entry.delay:
            await asyncio.sleep(entry.delay)
        headers: dict[str, Any] = dict(entry.headers)
        return httpx.Response(entry.status, headers=headers, content=entry.body)
