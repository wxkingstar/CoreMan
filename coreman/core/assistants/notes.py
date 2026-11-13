"""个人知识库：按路径存取的页面。

路径、标题、摘要、正文四列登记在 `sealed.COLUMNS`：经 ORM 写入时由 flush 钩子封存，这里读出
的行一律显式还原。唯一性靠 `path_hmac`：规范化并折叠大小写之后的路径，用所有者的数据密钥算
带密钥摘要，只拿到数据库的人看不出有哪些页。

调用方负责提交。这里不写日志：路径与标题本身就是个人内容。
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.db.models import PAGE_AUTHORS, AssistantPage

MAX_PAGES = 2000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PAGE_BYTES = 200 * 1024
MAX_PATH_CHARS = 200
MAX_PATH_SEGMENTS = 8
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 300
# `sealed.content_mac` 的用途标签：路径摘要与别的带密钥摘要（审批单去重）互不相通。
PATH_PURPOSE = "assistant_pages.path"
_SLASHES = re.compile(r"/+")


class NotesError(Exception):
    """知识库拒绝了这次操作；`code` 给模型看，`detail` 是它改正时用得上的数。"""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, **self.detail}


@dataclass(frozen=True)
class Entry:
    """一页的条目：索引里的一行，也是写入之后的回执。"""

    path: str
    title: str
    summary: str
    pinned: bool
    version: int
    size: int
    updated_at: datetime


@dataclass(frozen=True)
class Page(Entry):
    body: str


def normalize(path: str) -> tuple[str, str]:
    """返回（显示用的路径, 唯一性用的键）。键折叠了大小写，显示用的保留写法。

    Raises:
        NotesError: `invalid_path`
    """
    shown = unicodedata.normalize("NFKC", path).replace("\\", "/")
    shown = _SLASHES.sub("/", "/".join(part.strip() for part in shown.split("/"))).strip("/")
    parts = shown.split("/") if shown else []
    if (
        not parts
        or len(shown) > MAX_PATH_CHARS
        or len(parts) > MAX_PATH_SEGMENTS
        or any(part in (".", "..") for part in parts)
        or any(unicodedata.category(char)[0] == "C" for char in shown)
    ):
        raise NotesError("invalid_path")
    return shown, shown.casefold()


async def _path_key(session: AsyncSession, bot_id: uuid.UUID, key: str) -> str:
    try:
        return await sealed.content_mac(session, bot_id, PATH_PURPOSE, key)
    except sealed.ContentLocked:
        raise NotesError("content_locked") from None
    except ValueError:
        raise NotesError("assistant_only") from None


async def _load(session: AsyncSession, bot_id: uuid.UUID, mac: str) -> AssistantPage | None:
    """按路径摘要取一页并还原。重新从库里取：会话里可能留着这一行更早的样子。"""
    row: AssistantPage | None = await session.scalar(
        select(AssistantPage)
        .where(AssistantPage.bot_id == bot_id, AssistantPage.path_hmac == mac)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    await sealed.open_row(session, row)
    if sealed.is_locked(row):
        raise NotesError("content_locked")
    return row


def _entry(row: AssistantPage) -> Entry:
    return Entry(
        path=row.path_enc,
        title=row.title_enc,
        summary=row.summary_enc,
        pinned=row.pinned,
        version=row.version,
        size=row.size,
        updated_at=row.updated_at,
    )


def _default_title(shown: str) -> str:
    return shown.rsplit("/", 1)[-1].removesuffix(".md")[:MAX_TITLE_CHARS]


async def read(session: AsyncSession, bot_id: uuid.UUID, path: str) -> Page | None:
    """读一页；没有这一页返回 None。

    Raises:
        NotesError: `invalid_path`、`content_locked`、`assistant_only`
    """
    _, key = normalize(path)
    row = await _load(session, bot_id, await _path_key(session, bot_id, key))
    if row is None:
        return None
    entry = _entry(row)
    return Page(
        path=entry.path,
        title=entry.title,
        summary=entry.summary,
        pinned=entry.pinned,
        version=entry.version,
        size=entry.size,
        updated_at=entry.updated_at,
        body=row.body_enc,
    )


async def write(
    session: AsyncSession,
    bot_id: uuid.UUID,
    path: str,
    *,
    body: str,
    summary: str,
    title: str | None = None,
    pinned: bool | None = None,
    version: int = 0,
    updated_by: str = "assistant",
) -> Entry:
    """新建一页，或把已有的一页整页替换。

    Args:
        title: 省略或为空时，新页取路径最后一段，已有的页保持原标题
        pinned: 省略时，新页不置顶，已有的页保持原样
        version: 调用方读到的版本；0 表示「我认为这页还不存在」
        updated_by: `PAGE_AUTHORS` 之一

    Raises:
        NotesError: `invalid_path`、`invalid_page`、`page_too_large`、`page_exists`、
            `page_not_found`、`version_conflict`、`too_many_pages`、`notes_full`、
            `content_locked`、`assistant_only`
    """
    shown, key = normalize(path)
    size = len(body.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise NotesError("page_too_large", limit_bytes=MAX_PAGE_BYTES, size_bytes=size)
    summary = " ".join(summary.split())
    title = " ".join(title.split()) if title else ""
    if (
        not body.strip()
        or len(summary) > MAX_SUMMARY_CHARS
        or len(title) > MAX_TITLE_CHARS
        or updated_by not in PAGE_AUTHORS
    ):
        raise NotesError("invalid_page")
    mac = await _path_key(session, bot_id, key)
    # 同一位助理的写入串行：版本检查与三个上限都是在这把锁里读了再写。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assistant_pages:{bot_id}"},
    )
    row = await _load(session, bot_id, mac)
    count, total = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(AssistantPage.size), 0))
            .select_from(AssistantPage)
            .where(AssistantPage.bot_id == bot_id)
        )
    ).one()
    # 数据库时钟：变更说明拿它与任务的认领时刻比。
    now: datetime = (await session.execute(select(func.now()))).scalar_one()
    if row is None:
        if version != 0:
            raise NotesError("page_not_found")
        if count >= MAX_PAGES:
            raise NotesError("too_many_pages", limit=MAX_PAGES)
        if total + size > MAX_TOTAL_BYTES:
            raise NotesError("notes_full", limit_bytes=MAX_TOTAL_BYTES)
        row = AssistantPage(
            bot_id=bot_id,
            path_hmac=mac,
            path_enc=shown,
            title_enc=title or _default_title(shown),
            summary_enc=summary,
            body_enc=body,
            size=size,
            pinned=bool(pinned),
            version=1,
            updated_by=updated_by,
            updated_at=now,
        )
        session.add(row)
    else:
        if version == 0:
            raise NotesError("page_exists", current_version=row.version)
        if version != row.version:
            raise NotesError("version_conflict", current_version=row.version)
        if total - row.size + size > MAX_TOTAL_BYTES:
            raise NotesError("notes_full", limit_bytes=MAX_TOTAL_BYTES)
        row.body_enc = body
        row.summary_enc = summary
        if title:
            row.title_enc = title
        if pinned is not None:
            row.pinned = pinned
        row.size = size
        row.version += 1
        row.updated_by = updated_by
        row.updated_at = now
    await session.flush()
    return _entry(row)


async def delete(session: AsyncSession, bot_id: uuid.UUID, path: str) -> bool:
    """删除一页；返回原来有没有这一页。

    Raises:
        NotesError: `invalid_path`、`content_locked`、`assistant_only`
    """
    _, key = normalize(path)
    mac = await _path_key(session, bot_id, key)
    gone = await session.execute(
        sql_delete(AssistantPage)
        .where(AssistantPage.bot_id == bot_id, AssistantPage.path_hmac == mac)
        .returning(AssistantPage.id)
    )
    return gone.first() is not None
