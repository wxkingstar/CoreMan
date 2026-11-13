"""个人知识库的五个工具：登记在助理工具注册表的 knowledge 组。

knowledge 组不受飞书授权代数影响，子任务也能用；分派时只认这一轮核验过的助理记录
（`scope.bot`），路径之外没有别的寻址方式，读写不到别人的页。

登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, TextPage, input_schema, text_page

# 路径的规则在 notes.normalize；这里只挡住明显过长的。
PagePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2 * notes.MAX_PATH_CHARS),
    Field(description="Page path such as people/zhang-san.md; case-insensitive"),
]


class Index(Arguments):
    folder: Annotated[str, StringConstraints(max_length=2 * notes.MAX_PATH_CHARS)] = Field(
        default="", description="Only pages under this folder, e.g. people"
    )
    limit: int = Field(default=200, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class Read(TextPage):
    path: PagePath


class Search(Arguments):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    limit: int = Field(default=10, ge=1, le=20)


class Write(Arguments):
    path: PagePath
    body: Annotated[str, StringConstraints(min_length=1, max_length=notes.MAX_PAGE_BYTES)] = Field(
        description="The whole page in Markdown; replaces what was there"
    )
    summary: Annotated[str, StringConstraints(max_length=notes.MAX_SUMMARY_CHARS)] = Field(
        description="One sentence shown in the index"
    )
    title: Annotated[str, StringConstraints(max_length=notes.MAX_TITLE_CHARS)] = Field(
        default="", description="Omit to keep the current title (new page: last path segment)"
    )
    pinned: bool | None = Field(
        default=None,
        description="Pinned pages are given to you at the start of every session. Pin, unpin or"
        " change a pinned page only in a turn the owner started, when the owner asks for it",
    )
    version: int = Field(
        default=0, ge=0, description="0 for a new page; otherwise the version you last read"
    )


class Delete(Arguments):
    path: PagePath


OWN: dict[str, tuple[type[Arguments], str]] = {
    "notes_index": (
        Index,
        "List the pages of the owner's private knowledge base: path, title, one-line summary,"
        " pinned, version. Paged; continue with offset = next_offset.",
    ),
    "notes_read": (
        Read,
        "Read one page by path. Long pages come in slices; continue with text_offset ="
        " next_offset.",
    ),
    "notes_search": (
        Search,
        "Keyword search over every page (space-separated terms, case-insensitive). Returns"
        " ranked hits with a snippet; read the page for the rest.",
    ),
    "notes_write": (
        Write,
        "Create a page or replace it whole. New page: version 0. Existing page: pass the"
        " version you read; on version_conflict read it again, merge and write once more.",
    ),
    "notes_delete": (Delete, "Delete one page by path."),
}
PIN_HINT = (
    "Pinned pages are rules for every session; they change only in a turn the owner started."
    " Nobody is present in this turn: leave the page as it is, write what you learned to a page"
    " that is not pinned, and tell the owner what you would change."
)
DEFINITIONS: list[dict[str, Any]] = [
    {"name": name, "description": text, "inputSchema": input_schema(args)}
    for name, (args, text) in OWN.items()
]


def _listed(entry: notes.Entry) -> dict[str, Any]:
    return {
        "path": entry.path,
        "title": entry.title,
        "summary": entry.summary,
        "pinned": entry.pinned,
        "version": entry.version,
        "updated_at": entry.updated_at.isoformat(),
    }


async def _index(session: AsyncSession, scope: Scope, args: Index) -> dict[str, Any]:
    entries = await notes.index(session, scope.bot.id, folder=args.folder)
    page = entries[args.offset : args.offset + args.limit]
    end = args.offset + len(page)
    return {
        "pages": [_listed(entry) for entry in page],
        "total": len(entries),
        "next_offset": end if end < len(entries) else None,
    }


async def _read(session: AsyncSession, scope: Scope, args: Read) -> dict[str, Any]:
    page = await notes.read(session, scope.bot.id, args.path)
    if page is None:
        return {"error": "page_not_found"}
    body, meta = text_page(page.body, args)
    return {**_listed(page), "body": body, **meta}


async def _search(session: AsyncSession, scope: Scope, args: Search) -> dict[str, Any]:
    hits = await notes.search(session, scope.bot.id, args.query, limit=args.limit)
    return {"hits": [asdict(hit) for hit in hits]}


async def _write(session: AsyncSession, scope: Scope, args: Write) -> dict[str, Any]:
    entry = await notes.write(
        session,
        scope.bot.id,
        args.path,
        body=args.body,
        summary=args.summary,
        title=args.title or None,
        pinned=args.pinned,
        version=args.version,
        updated_by="assistant",
        # 平台自己发起的轮次与子任务没有人在场：不能置顶，也动不了置顶页。
        owner_present=not scope.unattended,
    )
    return {"status": "saved", "path": entry.path, "version": entry.version, "pinned": entry.pinned}


async def _delete(session: AsyncSession, scope: Scope, args: Delete) -> dict[str, Any]:
    if await notes.delete(session, scope.bot.id, args.path, owner_present=not scope.unattended):
        return {"status": "deleted"}
    return {"error": "page_not_found"}


_RUN: dict[str, Callable[[AsyncSession, Scope, Any], Awaitable[dict[str, Any]]]] = {
    "notes_index": _index,
    "notes_read": _read,
    "notes_search": _search,
    "notes_write": _write,
    "notes_delete": _delete,
}


async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """执行一个知识库工具；调用方（MCP 入口）负责提交。`cipher` 用不上：内容由数据密钥加解密。"""
    found = OWN.get(name)
    if found is None or not is_assistant(scope.bot):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {
            "error": "invalid_tool_or_arguments",
            "invalid": approvals_service._problems(exc),
        }
    try:
        return await _RUN[name](session, scope, args)
    except notes.NotesError as exc:
        if exc.code == notes.PIN_REQUIRES_OWNER:
            return {**exc.payload(), "hint": PIN_HINT}
        return exc.payload()
