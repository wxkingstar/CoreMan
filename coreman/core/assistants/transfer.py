"""个人助理的导入与导出：把压缩包里的页面与待跟进并进知识库，或把现有的打成同样格式的包。

导入分两步，都由所有者本人在「我的助理」页上发起（写入一律 `updated_by="import"`、
`owner_present=True`）：先 `plan` 出一张清单给他看——只有路径、原因与是否置顶，不带正文——他
确认后再 `apply`。清单带一个 `digest`：确认时带回来，和服务端对着此刻的库重新算出来的对不上
（期间助理写过页面、换了一个包、换了「覆盖」选项），调用方就不写，让他重新看一遍。写入时每
一页仍带着清单里读到的版本号，清单与写入之间再被改动会撞上 `notes.write` 的版本检查。

这里只 flush、不提交：调用方把写入和访问记录放在一个事务里，任何一步失败整体回滚。压缩包的
格式与安全检查在 `archive.py`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, notes, sealed
from coreman.core.assistants.archive import FollowupItem, PageFile, Parsed
from coreman.core.assistants.followups import FollowupError
from coreman.core.assistants.notes import NotesError

# 清单与写入之间这一页被别人动过：让所有者重新预览。
_MOVED = frozenset({"page_exists", "page_not_found", "version_conflict"})


class TransferError(Exception):
    """这次导入或导出做不了；`code` 是原因代码，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PagePlan:
    """要写的一页。`version` 为 0 是新建，否则是清单里读到的、要覆盖的那一版。"""

    path: str
    title: str | None
    summary: str
    pinned: bool
    body: str
    version: int


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


@dataclass(frozen=True)
class Plan:
    create: list[PagePlan]
    overwrite: list[PagePlan]
    skip: list[Skipped]
    followups: list[FollowupItem]
    # （清单里的位置, 原因）
    followups_skip: list[tuple[int, str]]
    digest: str
    # 包里跳过没读的系统文件个数（`.DS_Store` 之类）。
    ignored: int = 0


def _squash(value: str | None) -> str:
    return " ".join((value or "").split())


def _checked(file: PageFile) -> tuple[str, str, str | None]:
    """（给人看的路径, 唯一性用的键, 不能导入的原因）；能导入时原因为 None。"""
    if file.problem is not None:
        return file.path[: notes.MAX_PATH_CHARS], "", file.problem
    try:
        shown, key = notes.normalize(file.path)
    except NotesError:
        return file.path[: notes.MAX_PATH_CHARS], "", "invalid_path"
    if len(file.body.encode("utf-8")) > notes.MAX_PAGE_BYTES:
        return shown, key, "page_too_large"
    if not file.body.strip():
        return shown, key, "empty_page"
    if (
        len(_squash(file.summary)) > notes.MAX_SUMMARY_CHARS
        or len(_squash(file.title)) > notes.MAX_TITLE_CHARS
    ):
        return shown, key, "invalid_page"
    return shown, key, None


async def plan(
    session: AsyncSession,
    bot_id: uuid.UUID,
    parsed: Parsed,
    *,
    overwrite: bool,
    today: date,
    fingerprint: str,
) -> Plan:
    """对着此刻的库，给这个包出一张清单；不写任何页面与待跟进。

    Args:
        overwrite: 已有的页是覆盖（真）还是跳过（假）
        today: 所有者时区的今天
        fingerprint: 压缩包的 SHA-256，算进 `digest`：确认时换了一个包也对不上

    Raises:
        TransferError: `content_locked`、`too_many_pages`、`notes_full`、`too_many_followups`
    """
    if not await sealed.key_ready(session, bot_id):
        raise TransferError("content_locked")
    existing = {
        notes.normalize(entry.path)[1]: entry for entry in await notes.index(session, bot_id)
    }
    create: list[PagePlan] = []
    replace: list[PagePlan] = []
    skip: list[Skipped] = []
    seen: set[str] = set()
    total = sum(entry.size for entry in existing.values())
    for file in parsed.pages:
        shown, key, reason = _checked(file)
        if reason is None and key in seen:
            reason = "duplicate_path"
        current = existing.get(key) if reason is None else None
        if current is not None and not overwrite:
            reason = "exists"
        if reason is not None:
            skip.append(Skipped(shown, reason))
            continue
        seen.add(key)
        total += len(file.body.encode("utf-8")) - (current.size if current else 0)
        if current is None:
            create.append(
                PagePlan(shown, file.title, _squash(file.summary), bool(file.pinned), file.body, 0)
            )
        else:
            # 文件没写的项保持原样：摘要、置顶沿用现有的，标题不给（`notes.write` 不动它）。
            replace.append(
                PagePlan(
                    shown,
                    file.title,
                    current.summary if file.summary is None else _squash(file.summary),
                    current.pinned if file.pinned is None else file.pinned,
                    file.body,
                    current.version,
                )
            )
    if len(existing) + len(create) > notes.MAX_PAGES:
        raise TransferError("too_many_pages")
    if total > notes.MAX_TOTAL_BYTES:
        raise TransferError("notes_full")
    fresh: list[FollowupItem] = []
    passed: list[tuple[int, str]] = []
    if parsed.followups:
        live = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
        taken = {(row.category, row.title_enc) for row in live}
        for item in parsed.followups:
            if item.problem is not None:
                passed.append((item.index, item.problem))
            elif (item.category, item.title) in taken:
                # 未结的清单里已经有同一类、同一个标题的一条。
                passed.append((item.index, "duplicate"))
            else:
                taken.add((item.category, item.title))
                fresh.append(item)
        if len(live) + len(fresh) > followups.MAX_OPEN:
            raise TransferError("too_many_followups")
    stamp = json.dumps(
        [
            fingerprint,
            overwrite,
            [page.path for page in create],
            [[page.path, page.version] for page in replace],
            [[item.path, item.reason] for item in skip],
            [item.index for item in fresh],
            passed,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(stamp.encode("utf-8")).hexdigest()
    return Plan(create, replace, skip, fresh, passed, digest, parsed.ignored)


async def apply(session: AsyncSession, bot_id: uuid.UUID, planned: Plan, *, today: date) -> None:
    """照清单写入。只 flush；抛错时调用方回滚，已经写了的一并作废。

    Raises:
        TransferError: `plan_changed`（清单之后有页面被动过）、`too_many_pages`、`notes_full`、
            `too_many_followups`、`content_locked` 等核心模块给的原因代码
    """
    try:
        for page in (*planned.create, *planned.overwrite):
            await notes.write(
                session,
                bot_id,
                page.path,
                body=page.body,
                summary=page.summary,
                title=page.title,
                pinned=page.pinned,
                version=page.version,
                updated_by="import",
                owner_present=True,
            )
        for item in planned.followups:
            row = await followups.add(
                session,
                bot_id,
                category=item.category,
                title=item.title,
                notes=item.notes,
                review_on=item.review_on,
                due_on=item.due_on,
            )
            if item.snoozed and item.review_on is not None and item.review_on > today:
                await followups.snooze(session, bot_id, row.seq, item.review_on, today=today)
    except NotesError as exc:
        raise TransferError("plan_changed" if exc.code in _MOVED else exc.code) from None
    except FollowupError as exc:
        raise TransferError(exc.code) from None


def preview(planned: Plan) -> dict[str, Any]:
    """给所有者看的清单：路径、原因、是否置顶与条数；没有标题、摘要与正文。

    `ignored` 是包里跳过没读的系统文件个数，只给一个数。
    """
    return {
        "digest": planned.digest,
        "ignored": planned.ignored,
        "create": [{"path": page.path, "pinned": page.pinned} for page in planned.create],
        "overwrite": [{"path": page.path, "pinned": page.pinned} for page in planned.overwrite],
        "skip": [{"path": item.path, "reason": item.reason} for item in planned.skip],
        "followups": {
            "create": len(planned.followups),
            "skip": [
                {"index": index, "reason": reason} for index, reason in planned.followups_skip
            ],
        },
    }


def counts(planned: Plan) -> str:
    """访问记录里的对象：只有四个数。"""
    return (
        f"new={len(planned.create)},over={len(planned.overwrite)},"
        f"skip={len(planned.skip)},fu={len(planned.followups)}"
    )


async def export(session: AsyncSession, bot_id: uuid.UUID, *, today: date) -> tuple[bytes, str]:
    """全部知识页与未结的待跟进（含推后的），打成与导入相同格式的 zip。

    Returns:
        （压缩包, 访问记录里的对象——只有两个数）

    Raises:
        TransferError: `content_locked`
    """
    if not await sealed.key_ready(session, bot_id):
        raise TransferError("content_locked")
    pages = await notes.dump(session, bot_id)
    live = await followups.list_(session, bot_id, "open", today=today, limit=followups.MAX_OPEN)
    items = [
        {
            "category": row.category,
            "title": row.title_enc,
            "notes": row.notes_enc,
            "review_on": row.review_on.isoformat() if row.review_on else None,
            "due_on": row.due_on.isoformat() if row.due_on else None,
            "status": row.status,
        }
        for row in live
    ]
    files = [
        (
            page.path,
            archive.page_text(
                title=page.title, summary=page.summary, pinned=page.pinned, body=page.body
            ),
        )
        for page in pages
    ]
    # 压缩是纯计算，装满的知识库有二十多 MB：放到线程里，不占事件循环。
    data = await asyncio.to_thread(archive.build, files, archive.followups_text(items))
    return data, f"pages={len(pages)},followups={len(items)}"
