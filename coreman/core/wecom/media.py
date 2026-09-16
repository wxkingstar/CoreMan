"""企微多媒体资源：下载 + AES-256-CBC 解密 + 类型/文件名判定。

全程在内存里完成，不落盘；`url`/`aeskey`/明文字节都不进日志。三个超时/上限常量固定为：
图片 10 秒、文件 30 秒、100 MB。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_BYTES = 100 * 1024 * 1024
IMAGE_TIMEOUT = 10.0
FILE_TIMEOUT = 30.0
_CHUNK = 64 * 1024
_RFC5987 = re.compile(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", re.IGNORECASE)
_PLAIN = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class MediaError(Exception):
    """媒体处理失败；`reason` 是稳定的机器可读原因，给 chat_logs.error_code 与文案分支用。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str
    filename: str | None


def derive_key(aeskey: str) -> bytes:
    """按形状判断密钥格式：32 字符直接取 UTF-8；64 位十六进制；43 位 base64（补一个 `=`）。"""
    if len(aeskey) == 32 and aeskey.isascii():
        return aeskey.encode()
    if len(aeskey) == 64 and _HEX64.match(aeskey):
        return bytes.fromhex(aeskey)
    if len(aeskey) == 43:
        try:
            key = base64.b64decode(aeskey + "=", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaError("invalid_key", "base64") from exc
        if len(key) == 32:
            return key
    raise MediaError("invalid_key", f"len={len(aeskey)}")


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """AES-256-CBC，IV = key[:16]，PKCS#7 去填充（填充值 1..32）。"""
    key = derive_key(aeskey)
    if not data or len(data) % 16:
        raise MediaError("decrypt_failed", "length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if pad < 1 or pad > 32 or pad > len(plain) or plain[-pad:] != bytes([pad]) * pad:
        raise MediaError("decrypt_failed", "padding")
    return plain[:-pad]


def sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _zip_kind(data: bytes) -> str:
    # 坏中央目录抛什么由 CPython 版本决定：BadZipFile / OSError 之外还见过 NotImplementedError
    # ("zip file version 25.5")、UnicodeDecodeError、struct.error。这里只是给字节取个名。
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except Exception:  # noqa: BLE001 取名不该抛，否则会越过 MediaError 那组封闭 reason
        return "file.zip"
    for prefix, name in (("xl/", "file.xlsx"), ("word/", "file.docx"), ("ppt/", "file.pptx")):
        if any(n.startswith(prefix) for n in names):
            return name
    return "file.zip"


def _magic_name(data: bytes) -> str:
    """只看字节内容取名；调用方负责兜住异常（见 guess_filename）。"""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "file.pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "file.xls"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(data)
    if head.startswith(b"\x89PNG"):
        return "file.png"
    if head.startswith(b"\xff\xd8\xff"):
        return "file.jpg"
    try:
        data[:1024].decode("utf-8")
    except UnicodeDecodeError:
        return "file.bin"
    return "file.txt"


def guess_filename(*, given: str | None, content_disposition: str | None, data: bytes) -> str:
    """三级回退：回调给的名字 > Content-Disposition（含 RFC 5987）> 魔数。

    魔数这一级必须是全函数：MediaError 的 reason 是封闭集合，取名再怎么失败也只能降级成通用名，
    不能让裸异常从 fetch_file 逃出去。
    """
    if given:
        return given
    if content_disposition:
        m = _RFC5987.search(content_disposition)
        if m:
            return unquote(m.group(1).strip())
        m = _PLAIN.search(content_disposition)
        if m:
            return m.group(1).strip()
    try:
        return _magic_name(data)
    except Exception:  # noqa: BLE001 同上：取名是回退路径，任何异常都只降级
        return "file.zip" if data[:4] == b"PK\x03\x04" else "file.bin"


def mime_for(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def data_uri(media: Media) -> str:
    return f"data:{media.mime};base64,{base64.b64encode(media.data).decode()}"


class MediaFetcher:
    """下载并解密企微媒体；`http` 可注入（测试用 MockTransport）。"""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        max_bytes: int = MAX_BYTES,
        image_timeout: float = IMAGE_TIMEOUT,
        file_timeout: float = FILE_TIMEOUT,
    ) -> None:
        self._http = http or httpx.AsyncClient(follow_redirects=False)
        self._owns = http is None
        self.max_bytes = max_bytes
        self.image_timeout = image_timeout
        self.file_timeout = file_timeout

    async def aclose(self) -> None:
        if self._owns:
            await self._http.aclose()

    async def fetch_image(self, ref: dict[str, Any]) -> Media:
        data, _headers = await self._download(ref, self.image_timeout)
        plain = decrypt_media(data, str(ref.get("aeskey") or ""))
        return Media(plain, sniff_image_mime(plain), None)

    async def fetch_file(self, ref: dict[str, Any], *, filename: str | None) -> Media:
        data, headers = await self._download(ref, self.file_timeout)
        plain = decrypt_media(data, str(ref.get("aeskey") or ""))
        name = guess_filename(
            given=filename, content_disposition=headers.get("content-disposition"), data=plain
        )
        return Media(plain, mime_for(name), name)

    async def _download(self, ref: dict[str, Any], budget: float) -> tuple[bytes, httpx.Headers]:
        url = str(ref.get("url") or "")
        aeskey = str(ref.get("aeskey") or "")
        if not url or not aeskey:
            raise MediaError("download_failed", "missing url or key")
        derive_key(aeskey)  # 先验密钥形状，坏密钥不必白下载一遍
        try:
            # httpx 的 `timeout=` 只管单次读写间隔，且注入的 MockTransport 根本不受它约束：
            # 整段下载再罩一层 asyncio 截止时间，慢而不断的服务端也一定会在预算内收场。
            async with asyncio.timeout(budget):
                async with self._http.stream("GET", url, timeout=budget) as resp:
                    if resp.status_code != 200:
                        raise MediaError("download_failed", f"status={resp.status_code}")
                    declared = resp.headers.get("content-length")
                    # `isdecimal()` 与 `int()` 精确对齐；`isdigit()` 会放行「²」这类字符。
                    if declared and declared.isdecimal() and int(declared) > self.max_bytes:
                        raise MediaError("too_large", f"declared={declared}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise MediaError("too_large", f"read>{self.max_bytes}")
                        chunks.append(chunk)
                    return b"".join(chunks), resp.headers
        except TimeoutError as exc:
            raise MediaError("timeout", f"budget={budget}s") from exc
        except httpx.TimeoutException as exc:
            raise MediaError("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise MediaError("download_failed", type(exc).__name__) from exc
