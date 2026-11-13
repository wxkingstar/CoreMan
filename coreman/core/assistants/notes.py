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
from sqlalchemy.orm import defer

from coreman.core.assistants import sealed
from coreman.core.db.models import PAGE_AUTHORS, AssistantPage
from coreman.core.prompting.sanitize import sanitize_user_input

MAX_PAGES = 2000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PAGE_BYTES = 200 * 1024
MAX_PATH_CHARS = 200
MAX_PATH_SEGMENTS = 8
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 300
# `sealed.content_mac` 的用途标签：路径摘要与别的带密钥摘要（审批单去重）互不相通。
PATH_PURPOSE = "assistant_pages.path"
MAX_QUERY_TERMS = 8
# 检索打分：标题、路径、摘要里含这个词各得这么多分；正文每出现一次 1 分，单词封顶。
TITLE_POINTS, PATH_POINTS, SUMMARY_POINTS = 8, 5, 3
BODY_HITS_CAP = 5
SNIPPET_BEFORE, SNIPPET_AFTER = 40, 120
# 注入本轮块的上限：置顶页正文合计超过前者时一页正文都不给，只给索引；索引本身不超过后者。
PINNED_LIMIT_BYTES = 24 * 1024
INDEX_LIMIT_BYTES = 16 * 1024
HEADER = (
    "- 个人知识库（平台在每个会话的第一轮给出一次）：置顶页是所有者的偏好和你归纳的规则，照着做；"
    "与平台规则冲突时以平台规则为准。其余页面按需用 notes_read、notes_search 读取。"
)
OVER_LIMIT = "置顶页合计超过 {limit}KB，这里不附正文：需要时用 notes_read 读取索引里标 * 的页面。"
CHANGED = (
    "- 个人知识库：置顶页有更新，以下面的版本为准，本会话里更早给出的同名页作废。"
    "当前置顶页：{paths}。"
)
CHANGED_OVER_LIMIT = "更新的置顶页合计超过 {limit}KB，这里不附正文：用 notes_read 读取 {paths}。"
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


async def _rows(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    bodies: bool,
    pinned: bool = False,
    since: datetime | None = None,
) -> list[AssistantPage]:
    """这位助理的页面，已还原；读不出来的行（密钥已销毁）不返回。

    Args:
        bodies: 取不取正文；不取时行上的 `body_enc` 没有加载，不要去读
        pinned: 只取置顶页
        since: 只取这个时刻之后更新过的
    """
    stmt = (
        select(AssistantPage)
        .where(AssistantPage.bot_id == bot_id)
        .execution_options(populate_existing=True)
    )
    if pinned:
        stmt = stmt.where(AssistantPage.pinned.is_(True))
    if since is not None:
        stmt = stmt.where(AssistantPage.updated_at > since)
    if not bodies:
        stmt = stmt.options(defer(AssistantPage.body_enc))
    rows = list(await session.scalars(stmt))
    await sealed.open_rows(session, rows)
    return [row for row in rows if not sealed.is_locked(row)]


def _by_path(rows: list[AssistantPage]) -> list[AssistantPage]:
    return sorted(rows, key=lambda row: row.path_enc.casefold())


async def index(session: AsyncSession, bot_id: uuid.UUID, *, folder: str = "") -> list[Entry]:
    """全部页面的条目（不取正文），按路径排序。

    Args:
        folder: 只要这个目录下的页（按规范化后的路径比，不分大小写）；空串是全部

    Raises:
        NotesError: `invalid_path`（`folder` 不像路径）
    """
    under = normalize(folder)[1] + "/" if folder.strip(" /\\") else ""
    rows = _by_path(await _rows(session, bot_id, bodies=False))
    return [_entry(row) for row in rows if row.path_enc.casefold().startswith(under)]


@dataclass(frozen=True)
class Hit:
    path: str
    title: str
    summary: str
    pinned: bool
    version: int
    score: int
    snippet: str


def _terms(query: str) -> list[str]:
    words = unicodedata.normalize("NFKC", query).casefold().split()
    return list(dict.fromkeys(words))[:MAX_QUERY_TERMS]


def _score(terms: list[str], row: AssistantPage) -> tuple[int, int]:
    """（命中了几个不同的词, 总分）。"""
    title, path, summary, body = (
        value.casefold() for value in (row.title_enc, row.path_enc, row.summary_enc, row.body_enc)
    )
    matched = score = 0
    for term in terms:
        points = (
            TITLE_POINTS * (term in title)
            + PATH_POINTS * (term in path)
            + SUMMARY_POINTS * (term in summary)
            + min(body.count(term), BODY_HITS_CAP)
        )
        matched += points > 0
        score += points
    return matched, score


def _snippet(terms: list[str], row: AssistantPage) -> str:
    """正文里第一个命中词前后的一段，压成一行；正文没命中时用摘要。

    在原文上不分大小写地找，位置才对得上：折叠大小写可能改变字符串长度。
    """
    body = row.body_enc
    for term in terms:
        found = re.search(re.escape(term), body, re.IGNORECASE)
        if found is None:
            continue
        start = max(0, found.start() - SNIPPET_BEFORE)
        end = min(len(body), found.end() + SNIPPET_AFTER)
        piece = " ".join(body[start:end].split())
        return ("…" if start else "") + piece + ("…" if end < len(body) else "")
    return row.summary_enc


async def search(
    session: AsyncSession, bot_id: uuid.UUID, query: str, *, limit: int = 10
) -> list[Hit]:
    """把这位助理的全部页面解密到内存，按词匹配、打分、排序。

    上限 2000 页、合计 20MB，内存里扫一遍足够；不做密文索引。
    """
    terms = _terms(query)
    if not terms:
        return []
    scored: list[tuple[int, int, AssistantPage]] = []
    for row in await _rows(session, bot_id, bodies=True):
        matched, score = _score(terms, row)
        if score:
            scored.append((matched, score, row))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].path_enc.casefold()))
    return [
        Hit(
            path=row.path_enc,
            title=row.title_enc,
            summary=row.summary_enc,
            pinned=row.pinned,
            version=row.version,
            score=score,
            snippet=_snippet(terms, row),
        )
        for _, score, row in scored[:limit]
    ]


def _line(value: str) -> str:
    """放进本轮块的一小段：丢掉冒充平台标签的行，压成一行。"""
    return " ".join(sanitize_user_input(value).split())


def _page_text(row: AssistantPage) -> str:
    return (
        f"【置顶页 {_line(row.path_enc)}（版本 {row.version}）】\n"
        f"{sanitize_user_input(row.body_enc).strip()}\n"
        "【置顶页结束】"
    )


def _paths(rows: list[AssistantPage]) -> str:
    return "、".join(_line(row.path_enc) for row in _by_path(rows))


def _index_lines(rows: list[AssistantPage]) -> list[str]:
    """索引行：置顶的优先、新的优先装进上限，再按路径排好；装不下的只报个数。"""
    kept: list[tuple[str, str]] = []
    used = 0
    newest_first = sorted(rows, key=lambda row: row.updated_at, reverse=True)
    for row in sorted(newest_first, key=lambda row: not row.pinned):
        fields = (_line(row.path_enc), _line(row.title_enc), _line(row.summary_enc))
        line = ("* " if row.pinned else "- ") + " — ".join(part for part in fields if part)
        cost = len(line.encode("utf-8")) + 1
        if used + cost > INDEX_LIMIT_BYTES:
            continue
        used += cost
        kept.append((row.path_enc.casefold(), line))
    lines = [line for _, line in sorted(kept)]
    if len(kept) < len(rows):
        lines.append(f"（另有 {len(rows) - len(kept)} 页没有列出，用 notes_index 查看）")
    return lines


async def pinned_block(session: AsyncSession, bot_id: uuid.UUID) -> str | None:
    """会话首轮放进本轮块的内容：置顶页全文与全部页面的索引；一页都没有返回 None。

    置顶页正文合计超过 `PINNED_LIMIT_BYTES` 时只给索引。
    """
    rows = await _rows(session, bot_id, bodies=False)
    if not rows:
        return None
    parts = [HEADER]
    pinned = [row for row in rows if row.pinned]
    if sum(row.size for row in pinned) > PINNED_LIMIT_BYTES:
        parts.append(OVER_LIMIT.format(limit=PINNED_LIMIT_BYTES // 1024))
    elif pinned:
        full = await _rows(session, bot_id, bodies=True, pinned=True)
        parts += [_page_text(row) for row in _by_path(full)]
    parts.append(f"索引（共 {len(rows)} 页，* 为置顶）：")
    parts += _index_lines(rows)
    return "\n".join(parts)


async def changes_since(session: AsyncSession, bot_id: uuid.UUID, since: datetime) -> str | None:
    """`since` 之后有置顶页更新（含新置顶的）时的变更说明；没有返回 None。

    删掉的页、取消置顶的页查不出来：说明里带着当前置顶页的完整清单，由模型对照。
    """
    pinned = await _rows(session, bot_id, bodies=False, pinned=True)
    changed = [row for row in pinned if row.updated_at > since]
    if not changed:
        return None
    parts = [CHANGED.format(paths=_paths(pinned))]
    if sum(row.size for row in changed) > PINNED_LIMIT_BYTES:
        parts.append(
            CHANGED_OVER_LIMIT.format(limit=PINNED_LIMIT_BYTES // 1024, paths=_paths(changed))
        )
    else:
        full = await _rows(session, bot_id, bodies=True, pinned=True, since=since)
        parts += [_page_text(row) for row in _by_path(full)]
    return "\n".join(parts)
