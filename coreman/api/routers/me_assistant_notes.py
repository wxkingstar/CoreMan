"""「我的助理」页的知识库：索引、读一页、新建或整页替换、删除。

路径本身是个人内容：读、写、删都把它放在请求体里，不放进网址（网址会进反向代理与应用的访问
日志）。所以读与删是 `POST /notes/read`、`POST /notes/delete`，索引也不收目录参数。

这是所有者看见「置顶了什么」并修改置顶页的唯一界面：写入一律 `owner_present=True`、
`updated_by="owner"`。核心逻辑在 `coreman/core/assistants/notes.py`。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import VERSION_CONFLICT, ApiError
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, notes, sealed
from coreman.core.assistants.notes import NotesError

router = APIRouter(prefix=PREFIX + "/notes", tags=["my-assistant"], dependencies=DEPENDENCIES)

# 请求体里路径的长度上限：规范化之前可能带多余的斜杠与空白，放宽到核心上限的两倍。
PATH_INPUT = notes.MAX_PATH_CHARS * 2
ERRORS: dict[str, tuple[int, str]] = {
    "invalid_path": (422, "路径不合法：最多 8 级、200 字，不能有「.」「..」这样的段或控制字符"),
    "invalid_page": (
        422,
        f"正文不能为空，摘要不超过 {notes.MAX_SUMMARY_CHARS} 字，"
        f"标题不超过 {notes.MAX_TITLE_CHARS} 字",
    ),
    "page_too_large": (422, f"单页不能超过 {notes.MAX_PAGE_BYTES // 1024}KB"),
    "page_exists": (409, "这个路径已经有一页了，请从列表里打开它再改"),
    "page_not_found": (404, "这一页不存在"),
    "too_many_pages": (409, f"知识库已达 {notes.MAX_PAGES} 页上限"),
    "notes_full": (409, "知识库合计已达 20MB 上限"),
    "content_locked": (409, "加密保存的个人内容已不可读取"),
    "assistant_only": (404, "你还没有个人助理"),
    "pin_requires_owner": (409, "置顶页只能由所有者本人修改"),
}


def _refuse(exc: NotesError) -> ApiError:
    if exc.code == "version_conflict":
        # 专用业务码：前端据此重新载入这一页，而不是原话提示。
        return ApiError(
            409,
            VERSION_CONFLICT,
            "这一页已被改动，请重新打开后再保存",
            [{"type": exc.code, **exc.detail}],
        )
    return refuse(exc.code, ERRORS)


def _entry(entry: notes.Entry) -> dict[str, Any]:
    out = asdict(entry)
    out["updated_at"] = entry.updated_at.isoformat()
    return out


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=PATH_INPUT)
    # 字节上限由核心函数判断（中文一字三字节）；这里按字符数挡住离谱的请求体。
    body: str = Field(max_length=notes.MAX_PAGE_BYTES)
    summary: str = Field(default="", max_length=notes.MAX_SUMMARY_CHARS * 4)
    title: str | None = Field(default=None, max_length=notes.MAX_TITLE_CHARS * 4)
    # 省略：新页不置顶，已有的页保持原样。
    pinned: StrictBool | None = None
    # 调用方读到的版本；0 表示「我认为这页还不存在」。
    version: int = Field(default=0, ge=0)


class PathIn(BaseModel):
    """读与删只带一个路径。"""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=PATH_INPUT)


@router.get("")
async def index(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        entries = await notes.index(session, own.bot.id)
    except NotesError as exc:
        raise _refuse(exc) from None
    # 密钥不可用时核心函数把读不出来的行都略过了：告诉页面这是「读不出来」而不是「没有」。
    locked = not await sealed.key_ready(session, own.bot.id)
    await access_log.record(session, own.bot.id, own.actor.id, "notes.index", "")
    data = {"items": [_entry(entry) for entry in entries], "locked": locked}
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/read")
async def read(
    body: PathIn, own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        page = await notes.read(session, own.bot.id, body.path)
    except NotesError as exc:
        raise _refuse(exc) from None
    if page is None:
        raise refuse("page_not_found", ERRORS)
    # 路径本身是个人内容，不进访问记录。
    await access_log.record(session, own.bot.id, own.actor.id, "notes.read", "page")
    data = _entry(page)
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}


@router.put("/page")
async def write(
    body: PageIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        entry = await notes.write(
            session,
            own.bot.id,
            body.path,
            body=body.body,
            summary=body.summary,
            title=body.title,
            pinned=body.pinned,
            version=body.version,
            updated_by="owner",
            owner_present=True,
        )
    except NotesError as exc:
        await session.rollback()
        raise _refuse(exc) from None
    data = _entry(entry)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/delete")
async def delete(
    body: PathIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        gone = await notes.delete(session, own.bot.id, body.path, owner_present=True)
    except NotesError as exc:
        await session.rollback()
        raise _refuse(exc) from None
    await session.commit()
    return {"code": 0, "data": {"deleted": gone}}
