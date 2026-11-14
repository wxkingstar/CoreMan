"""Large and file responses of proxied calls go to short-term object storage.

The model gets a preview and a link instead of a truncated body. Two kinds of link:

- Data (JSON, text) is business data: only this turn's runtime can download it, with the catalog
  capability it already holds (`/api/runtime/systems/objects/<id>`), so it can be processed with
  scripts without ever handing the business token to the runtime. Not meant for people.
- Files (attachments, binary content) are meant for people: a signed CoreMan link that expires
  after an hour and can be passed on.

The token is redacted from stored text the same way as from inline results.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from functools import partial
from typing import IO, Any
from urllib.parse import unquote

import httpx

from coreman.core.logging import get_logger
from coreman.core.object_store import LINK_TTL, disk_io, object_store
from coreman.core.systems_catalog import policy
from coreman.core.systems_catalog.context import Context
from coreman.core.timeutils import utcnow

log = get_logger(__name__)

# A successful text result longer than this is stored; the model gets a preview.
INLINE_MAX = 20_000
PREVIEW_MAX = 4_000
FILE_LINK_TTL = timedelta(hours=1)
OBJECT_PATH = "/api/runtime/systems/objects/"
CHUNK = 65_536
REDACTED = b"[REDACTED]"
_TEXT_TYPES = ("json", "text/", "xml", "yaml", "javascript", "x-www-form-urlencoded")
_FILENAME = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)|filename\s*=\s*\"?([^\";]+)\"?", re.I)


def content_type(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", "")).split(";", 1)[0].strip().lower()


def is_file(response: httpx.Response) -> bool:
    base = content_type(response)
    textual = not base or any(marker in base for marker in _TEXT_TYPES)
    return "attachment" in response.headers.get("content-disposition", "").lower() or not textual


def _filename(response: httpx.Response, fallback: str) -> str:
    match = _FILENAME.search(response.headers.get("content-disposition", ""))
    if match:
        name = unquote(match.group(1) or match.group(2) or "").strip()
        if name:
            return name
    return fallback


async def _chunks(spool: IO[bytes], secret: bytes) -> AsyncIterator[bytes]:
    """The spooled body from the start, with every occurrence of `secret` replaced, including
    one split across two chunks."""
    await disk_io(partial(spool.seek, 0))
    keep = max(len(secret) - 1, 0)
    carry = b""
    while chunk := await disk_io(partial(spool.read, CHUNK)):
        if not secret:
            yield chunk
            continue
        data = (carry + chunk).replace(secret, REDACTED)
        if len(data) > keep:
            yield data[: len(data) - keep]
            carry = data[len(data) - keep :]
        else:
            carry = data
    if carry:
        yield carry


def _preview(text: str, json_body: bool) -> tuple[Any, int | None]:
    from coreman.core.systems_catalog.proxy import fit_json

    if json_body:
        try:
            return fit_json(json.loads(text), PREVIEW_MAX)
        except ValueError:
            pass
    return text[:PREVIEW_MAX], -1


async def store(
    ctx: Context,
    *,
    system_key: str,
    operation_id: str,
    response: httpx.Response,
    spool: IO[bytes],
    head: bytes,
    complete: bool,
    secret: str,
) -> tuple[dict[str, Any], Any] | None:
    """(result for the model, stored object id); None when storage is unavailable or fails, so
    the caller falls back to the truncated inline result."""
    assert ctx.settings is not None
    file = is_file(response)
    base = content_type(response)
    extension = "json" if "json" in base else "txt"
    filename = _filename(response, f"{system_key}-{operation_id}.{'bin' if file else extension}")
    now = utcnow()
    try:
        row = await object_store(ctx.settings).put(
            ctx.session,
            _chunks(spool, b"" if file else secret.encode()),
            filename=filename,
            content_type=base or "application/octet-stream",
            now=now,
        )
    except Exception as exc:  # 存储不可用时退回截断的结果，不让一次查询整个失败。
        log.warning("system_call_spill_failed", error_type=type(exc).__name__)
        return None
    status = response.status_code
    if file:
        row.expires_at = now + FILE_LINK_TTL
        url = object_store(ctx.settings, row.backend).url(row)
        return {
            "status": status,
            "ok": True,
            "file": True,
            "stored": True,
            "filename": row.filename,
            "content_type": base or None,
            "bytes": row.size,
            "url": url,
            "expires_in": int(FILE_LINK_TTL.total_seconds()),
            "hint": "文件已存到临时中转，链接 1 小时内有效：可以直接发给用户，"
            "也可以在运行环境里下载后处理。",
        }, row.id
    row.expires_at = now + timedelta(seconds=min(ctx.token_ttl, LINK_TTL))
    text = head.decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    preview, omitted = _preview(text, "json" in base and complete)
    url = ctx.settings.public_base_url.rstrip("/") + OBJECT_PATH + str(row.id)
    return {
        "status": status,
        "ok": True,
        "stored": True,
        "content_type": base or None,
        "bytes": row.size,
        "preview": preview,
        **({"preview_omitted_items": omitted} if omitted and omitted > 0 else {}),
        "download": {
            "url": url,
            "curl": f'curl -sS -H "Authorization: Bearer ${policy.TOKEN_ENV}" "{url}" -o <文件名>',
        },
        "hint": "响应较大，完整结果已存为文件，preview 只是开头部分。需要完整数据时在运行环境里用 "
        "download.curl 下载后用脚本处理（如 jq、Python）；这个链接只在本轮由运行环境使用，"
        "不要发给用户。也可以改用分页、筛选参数缩小结果。",
    }, row.id
