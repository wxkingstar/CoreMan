"""「我的助理」页的导入与导出：所有者本人上传一个压缩包并进知识库与待跟进，或把它们打包下载。

导入分两步：`dry_run`（默认）只出清单，确认时带回清单的 `digest` 才真正写入；整个导入一个
事务，任何一步失败整体回滚。压缩包的格式与安全检查在 `coreman/core/assistants/archive.py`，
对着库出清单与写入在 `transfer.py`。出清单要读知识库的目录，所以预览也记一条访问记录。

上传按块读，一过上限就停并回 413（`_receive`）。框架在进到这里之前已经把请求体落到临时文件
（不在内存里），所以真正挡住「传上来」要靠反向代理的请求体上限，见 `docs/operations.md`。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, followups, notes, transfer
from coreman.core.assistants import archive as archive_format
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.archive import ArchiveError
from coreman.core.assistants.transfer import TransferError
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)

ARCHIVE_ERRORS: dict[str, tuple[int, str]] = {
    "archive_too_large": (413, "压缩包不能超过 20MB"),
    "invalid_archive": (422, "这不是一个读得出来的 zip 压缩包"),
    "too_many_entries": (422, f"压缩包里的页面文件不能超过 {archive_format.MAX_PAGE_FILES} 个"),
    "unpacked_too_large": (422, "压缩包解开之后太大了"),
    "unsafe_path": (422, "压缩包里有不安全的文件名（绝对路径、..、反斜杠或控制字符）"),
    "symlink_not_allowed": (422, "压缩包里不能有符号链接"),
    "encrypted_entry": (422, "压缩包里不能有加密的文件"),
    "nested_archive": (422, "压缩包里不能再套压缩包"),
    "unsupported_file": (422, "压缩包里只能有 .md 文件和根目录下的 followups.json"),
    "not_utf8": (422, "压缩包里有不是 UTF-8 文本的文件"),
    "invalid_followups": (422, "followups.json 的格式不对"),
}
TRANSFER_ERRORS: dict[str, tuple[int, str]] = {
    "content_locked": (409, "加密保存的个人内容已不可读取"),
    "too_many_pages": (409, f"导入之后会超过知识库 {notes.MAX_PAGES} 页的上限"),
    "notes_full": (409, "导入之后会超过知识库合计 20MB 的上限"),
    "too_many_followups": (409, f"导入之后未结的待跟进会超过 {followups.MAX_OPEN} 条的上限"),
    "plan_changed": (409, "预览之后内容有变化，请核对新的清单后再确认"),
}


# 读上传时一次取的块。
UPLOAD_CHUNK = 1024 * 1024


async def _receive(upload: UploadFile) -> bytes:
    """按块读上传的压缩包；累计超过上限立刻停下，不把整个文件读进内存之后才判断。

    Raises:
        ArchiveError: `archive_too_large`
    """
    limit = archive_format.MAX_ARCHIVE_BYTES
    parts: list[bytes] = []
    taken = 0
    while chunk := await upload.read(UPLOAD_CHUNK):
        taken += len(chunk)
        if taken > limit:
            raise ArchiveError("archive_too_large", limit_bytes=limit)
        parts.append(chunk)
    return b"".join(parts)


def _bad_archive(exc: ArchiveError) -> ApiError:
    status, message = ARCHIVE_ERRORS.get(exc.code, (422, "这个压缩包不能导入"))
    # 文件名是所有者自己这次上传的东西，只回给他本人，方便他改；不写进日志。
    name = exc.detail.get("name")
    return ApiError(status, status, f"{message}：{name}" if name else message, [{"type": exc.code}])


@router.post("/import")
async def import_archive(
    archive: UploadFile,
    dry_run: Literal["true", "false"] = Form("true"),
    overwrite: Literal["true", "false"] = Form("false"),
    expect: str = Form("", max_length=64),
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """导入一个 Markdown 压缩包。

    默认只预览（`dry_run=true`）：返回将新建、覆盖、跳过的清单与一个 `digest`，不写任何页面。
    确认时发 `dry_run=false` 并带上 `expect=<digest>`；对着此刻的库重新出的清单与它对不上
    （期间助理写过页面、换了包、换了选项）就 409 `plan_changed`，什么都不写。
    """
    # 之后可能回滚：回滚会让会话里的对象过期，先把要用的编号取出来。
    bot_id, actor_id = own.bot.id, own.actor.id
    try:
        data = await _receive(archive)
        # 解压与解析是纯计算，至多三十多 MB：放到线程里，不占事件循环。
        parsed = await asyncio.to_thread(archive_format.parse, data)
    except ArchiveError as exc:
        raise _bad_archive(exc) from None
    prefs = await assistant_settings.get(session, bot_id)
    today = assistant_settings.local_today(prefs, utcnow())
    try:
        plan = await transfer.plan(
            session,
            bot_id,
            parsed,
            overwrite=overwrite == "true",
            today=today,
            fingerprint=hashlib.sha256(data).hexdigest(),
        )
    except TransferError as exc:
        await session.rollback()
        raise refuse(exc.code, TRANSFER_ERRORS) from None
    summary, counted = transfer.preview(plan), transfer.counts(plan)
    if dry_run == "false" and expect == plan.digest:
        try:
            await transfer.apply(session, bot_id, plan, today=today)
        except TransferError as exc:
            # 一步写不进去：已经写了的全部回滚。清单读过库，照预览记一次。
            await session.rollback()
            await access_log.record(session, bot_id, actor_id, "import.preview", counted)
            await session.commit()
            raise refuse(exc.code, TRANSFER_ERRORS) from None
        # 写入与这条记录同一个事务。
        await access_log.record(session, bot_id, actor_id, "import.notes", counted)
        await session.commit()
        return {"code": 0, "data": {**summary, "dry_run": False}}
    await access_log.record(session, bot_id, actor_id, "import.preview", counted)
    await session.commit()
    if dry_run == "false":
        # 没带摘要，或摘要对不上：不预览就写不进去。
        raise refuse("plan_changed", TRANSFER_ERRORS)
    return {"code": 0, "data": {**summary, "dry_run": True}}
