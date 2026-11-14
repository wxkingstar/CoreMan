"""打回意见：所有者打回稿子时写的修改意见，由平台记进一张置顶页。

写入方是平台，不是模型：确认卡上的「打回重写」和审批系统的拒绝都落到 `record`。页面置顶，所以
每个会话的第一轮都带上，刚被打回的那一轮也会因为「置顶页有更新」当场带上。所有者可以在
「我的助理」页直接改这一页；页首（第一条 `## ` 之前）由他说了算，平台只动条目。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.crypto import DecryptError
from coreman.core.db.models import AssistantSettings
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)

PATH = "profile/feedback.md"
ARCHIVE = "profile/feedback-archive.md"
TITLE = "打回意见"
SUMMARY = "所有者打回稿子时写的修改意见，起草前逐条对照"
ARCHIVE_TITLE = "打回意见（归档）"
ARCHIVE_SUMMARY = "更早的打回意见，已不在置顶页里"
HEAD = (
    "所有者打回稿子时写的修改意见，新的在前。起草任何要以他名义发出的内容之前逐条对照；"
    "同类意见出现第二次，向他提议记成规则。\n\n"
)
MARK = "## "
# 置顶页合计 24KB：这一页只占 8KB，其余留给口吻与规则页。
BUDGET_BYTES = 8 * 1024
ARCHIVE_BYTES = 150 * 1024
DRAFT_MAX = 200
REASON_MAX = 500
WHAT_MAX = 80
RETRIES = 3
# 第一处调用里按这个顺序找正文。
_DRAFT_KEYS = ("text", "body", "content", "description", "comment", "summary", "title", "subject")


def _one(text: str) -> str:
    """压成单行：条目里的任何一段都造不出新的 `## ` 行。"""
    return " ".join(str(text).split())


def draft_of(calls: list[dict[str, Any]]) -> str:
    """暂存的调用里要发出去的那段文字，单行、截短；没有正文字段时为空串。"""
    arguments = (calls[0].get("arguments") if calls else None) or {}
    for key in _DRAFT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _one(value)[:DRAFT_MAX]
    return ""


def _entry(day: str, what: str, draft: str, reason: str) -> str:
    lines = [f"{MARK}{day} · {_one(what)[:WHAT_MAX]}"]
    if draft:
        lines.append(f"稿子：{_one(draft)[:DRAFT_MAX]}")
    lines.append(f"意见：{reason}")
    return "\n".join(lines) + "\n\n"


def split(body: str) -> tuple[str, list[str]]:
    """页首（第一条之前的文字）与各条目；条目各自以 `## ` 开头。"""
    head: list[str] = []
    entries: list[list[str]] = []
    for line in body.splitlines(keepends=True):
        if line.startswith(MARK):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            head.append(line)
    return "".join(head), ["".join(entry) for entry in entries]


def _fit(entries: list[str], budget: int) -> tuple[list[str], list[str]]:
    """从新到旧装进预算；装不下的（更旧的）另行返回。最新的一条总是留下。"""
    kept: list[str] = []
    used = 0
    for index, entry in enumerate(entries):
        size = len(entry.encode())
        if kept and used + size > budget:
            return kept, entries[index:]
        kept.append(entry)
        used += size
    return kept, []


async def _today(session: AsyncSession, bot_id: uuid.UUID) -> str:
    prefs = await session.scalar(
        select(AssistantSettings).where(AssistantSettings.bot_id == bot_id)
    )
    now = utcnow()
    day = assistant_settings.local_today(prefs, now) if prefs is not None else now.date()
    return day.isoformat()


async def _archive(session: AsyncSession, bot_id: uuid.UUID, overflow: list[str]) -> None:
    page = await notes.read(session, bot_id, ARCHIVE)
    old = split(page.body)[1] if page is not None else []
    kept, _ = _fit([*overflow, *old], ARCHIVE_BYTES)
    await notes.write(
        session,
        bot_id,
        ARCHIVE,
        body="".join(kept),
        summary=ARCHIVE_SUMMARY,
        title=ARCHIVE_TITLE if page is None else None,
        pinned=False if page is None else None,
        version=page.version if page is not None else 0,
        updated_by="owner",
    )


async def record(
    session: AsyncSession, bot_id: uuid.UUID, *, what: str, draft: str, reason: str
) -> bool:
    """把一条打回意见记到意见页最前面。调用方提交事务。

    Args:
        what: 平台生成的摘要（渠道与对象，如「私聊 → 李四」）
        draft: 被打回的稿子的摘录（`draft_of`）
        reason: 所有者写的意见原文

    Returns:
        记下了返回 True。意见为空、不是助理、数据密钥不可用、知识库已满或写入冲突到底都返回
        False：打回本身不受影响，只记一条不含内容的日志。
    """
    reason = _one(reason)[:REASON_MAX]
    if not reason:
        return False
    try:
        if not await sealed.key_ready(session, bot_id):
            return False
        new = _entry(await _today(session, bot_id), what, draft, reason)
        for _ in range(RETRIES):
            try:
                # 意见页与归档页同一个保存点：任一步失败都整体回滚，不丢条目。
                async with session.begin_nested():
                    page = await notes.read(session, bot_id, PATH)
                    head, old = split(page.body) if page is not None else (HEAD, [])
                    # 页首是所有者的字，不截断：页首超过预算一半时条目只占一半预算，
                    # 这时页面超过 8KB 只可能因为他自己写的页首。
                    room = max(BUDGET_BYTES - len(head.encode()), BUDGET_BYTES // 2)
                    kept, overflow = _fit([new, *old], room)
                    await notes.write(
                        session,
                        bot_id,
                        PATH,
                        body=head + "".join(kept),
                        summary=page.summary if page is not None else SUMMARY,
                        title=TITLE if page is None else None,
                        # 所有者取消过置顶的保持原样。
                        pinned=True if page is None else None,
                        version=page.version if page is not None else 0,
                        updated_by="owner",
                    )
                    if overflow:
                        await _archive(session, bot_id, overflow)
                return True
            except notes.NotesError as exc:
                if exc.code in ("version_conflict", "page_exists", "page_not_found"):
                    continue
                raise
    except (notes.NotesError, DecryptError, SQLAlchemyError) as exc:
        # 只记原因代码：异常文字里可能带内容。
        code = getattr(exc, "code", type(exc).__name__)
        log.warning("assistant_feedback_not_recorded", bot_id=str(bot_id), code=code)
        return False
    log.warning("assistant_feedback_not_recorded", bot_id=str(bot_id), code="conflict")
    return False
