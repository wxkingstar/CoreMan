"""回复里的远程图片：服务端取回、上传飞书换 image_key，卡片和富文本才能原生展示。"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import httpx

from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuError
from coreman.core.relay.safe_transport import PublicTransport
from coreman.runtime.gateway_feishu.cards import REMOTE_IMAGE

log = get_logger(__name__)

# 飞书上传图片接口上限 10 MB。
MAX_BYTES = 10 * 1024 * 1024
FETCH_SECONDS = 10.0
# 一条回复最多换这么多张，其余保留为链接，别让一轮投递卡在下载上。
PER_MESSAGE = 9
DOWNLOAD_LABEL = "查看原图"
_CACHE_SIZE = 256
# 取不到的地址过一会儿再试，不在每次刷新卡片时重复下载。
_RETRY_SECONDS = 300.0

Upload = Callable[[str, bytes, str], Awaitable[str]]


class ImageError(Exception):
    pass


def sniff(data: bytes) -> str | None:
    """只认飞书支持的图片格式，按文件头判断，不信响应头。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    return None


class RemoteImages:
    def __init__(self, upload: Upload, *, transport: httpx.AsyncBaseTransport | None = None):
        self._upload = upload
        self._transport = transport
        self._keys: OrderedDict[str, str] = OrderedDict()
        self._failed: dict[str, float] = {}

    async def localize(self, markdown: str) -> str:
        """远程图片换成 image_key，原地址另起一行保留为下载链接（正文已链过就不重复）。"""
        parts: list[str] = []
        last = 0
        for index, match in enumerate(REMOTE_IMAGE.finditer(markdown)):
            parts.append(markdown[last : match.start()])
            last = match.end()
            alt, url = match.group(1), match.group(2)
            key = await self.key_for(url) if index < PER_MESSAGE else None
            if key is None:
                parts.append(match.group(0))
                continue
            parts.append(f"![{alt}]({key})")
            if markdown.count(url) == 1:
                parts.append(f"\n[{DOWNLOAD_LABEL}]({url})")
        parts.append(markdown[last:])
        return "".join(parts)

    async def key_for(self, url: str) -> str | None:
        if url in self._keys:
            self._keys.move_to_end(url)
            return self._keys[url]
        now = time.monotonic()
        if self._failed.get(url, 0) > now:
            return None
        try:
            data = await self._fetch(url)
            kind = sniff(data)
            if kind is None:
                raise ImageError("not_image")
            key = await self._upload(f"image.{kind}", data, f"image/{kind}")
        except ImageError as exc:
            reason = str(exc)
        except (httpx.HTTPError, TimeoutError):
            reason = "fetch_failed"
        except FeishuError as exc:
            reason = f"upload_failed:{exc.code}"
        else:
            self._keys[url] = key
            while len(self._keys) > _CACHE_SIZE:
                self._keys.popitem(last=False)
            return key
        # 签名地址里带凭证，日志只记原因。
        log.warning("feishu_image_localize_failed", reason=reason)
        self._failed = {u: t for u, t in self._failed.items() if t > now}
        self._failed[url] = now + _RETRY_SECONDS
        return None

    async def _fetch(self, url: str) -> bytes:
        async with httpx.AsyncClient(
            transport=self._transport or PublicTransport(),
            trust_env=False,
            follow_redirects=True,
            max_redirects=3,
            timeout=httpx.Timeout(FETCH_SECONDS, connect=5),
        ) as http:
            async with asyncio.timeout(FETCH_SECONDS):
                async with http.stream("GET", url, headers={"Accept": "image/*"}) as response:
                    if response.status_code != 200:
                        raise ImageError(f"status_{response.status_code}")
                    declared = response.headers.get("content-length", "")
                    if declared.isdecimal() and int(declared) > MAX_BYTES:
                        raise ImageError("too_large")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ImageError("too_large")
                    return bytes(data)
