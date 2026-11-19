"""「我的助理」页的待跟进：所有者按四类看，直接在页面上新增、改、补充、改期、关闭。

核心逻辑在 `coreman/core/assistants/followups.py` 与 `owner_feedback.py`，这里只做入参、所有者
时区的「今天」、错误映射与提交。补充、改期、关闭都记一条所有者反馈，下班编译时进知识库。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import access_log, followups, owner_feedback, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.followups import FollowupError
from coreman.core.db.models import FOLLOWUP_CATEGORIES, AssistantFollowup
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX + "/followups", tags=["my-assistant"], dependencies=DEPENDENCIES)

ERRORS: dict[str, tuple[int, str]] = {
    "followup_not_found": (404, "这条待跟进不存在"),
    "followup_closed": (409, "这条待跟进已经关闭"),
    "invalid_category": (422, "分类只能是：等我决定、我答应的、等别人、提醒我"),
    "invalid_title": (422, f"标题不能为空，且不超过 {followups.MAX_TITLE} 字"),
    "invalid_notes": (422, f"备注不超过 {followups.MAX_NOTES} 字"),
    "too_many_followups": (409, f"未结的待跟进已达 {followups.MAX_OPEN} 条上限，先关掉一些"),
    "snooze_not_future": (422, "改期的日期必须晚于今天"),
    "invalid_feedback": (422, f"请填写内容，且不超过 {owner_feedback.MAX_NOTE} 字"),
    "invalid_close_code": (422, "请选择关闭原因"),
    "notes_full": (409, "这一条的备注已满，先精简备注再补充"),
    "content_locked": (409, "加密保存的个人内容已不可读取，不能再写入"),
    "nothing_to_change": (422, "至少修改一项"),
    "null_not_allowed": (422, "分类与标题不能清空"),
}


class AddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(max_length=32)
    # 长度由核心函数按去掉首尾空白之后的字数判断，这里只挡住离谱的请求体。
    title: str = Field(max_length=followups.MAX_TITLE * 4)
    notes: str | None = Field(default=None, max_length=followups.MAX_NOTES * 4)
    review_on: date | None = None
    due_on: date | None = None


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=followups.MAX_TITLE * 4)
    notes: str | None = Field(default=None, max_length=followups.MAX_NOTES * 4)
    review_on: date | None = None
    due_on: date | None = None


class CloseIn(BaseModel):
    """给了 `reason` 就按所有者选的原因关闭并记反馈；只给 `dropped` 是旧的办完 / 不再需要。"""

    model_config = ConfigDict(extra="forbid")
    dropped: StrictBool = False
    reason: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=owner_feedback.MAX_NOTE * 4)


class SnoozeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date
    note: str | None = Field(default=None, max_length=owner_feedback.MAX_NOTE * 4)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=owner_feedback.MAX_NOTE * 4)


async def _today(session: AsyncSession, own: Mine) -> date:
    """所有者时区里的今天：到期与推后都按它算。"""
    prefs = await assistant_settings.get(session, own.bot.id)
    return assistant_settings.local_today(prefs, utcnow())


def _item(row: AssistantFollowup, today: date) -> dict[str, Any]:
    out = followups.view(row)
    out["due_today"] = row.status == "open" and any(
        day is not None and day <= today for day in (row.review_on, row.due_on)
    )
    return out


def _seq(label: str) -> int:
    try:
        return followups.parse_label(label)
    except FollowupError as exc:
        raise refuse(exc.code, ERRORS) from None


@router.get("")
async def listed(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    today = await _today(session, own)
    rows = await followups.list_(session, own.bot.id, "open", today=today, limit=followups.MAX_OPEN)
    # 核心函数已经还原过；这里按查看者再核一次，不是所有者本人就全部收回。
    await sealed.open_rows(session, rows, viewer=own.actor.id)
    groups: dict[str, list[dict[str, Any]]] = {category: [] for category in FOLLOWUP_CATEGORIES}
    for row in rows:
        groups[row.category].append(_item(row, today))
    await access_log.record(session, own.bot.id, own.actor.id, "followups.read", "open")
    # 列表会把到了回看日的推后条目改回未结，并可能按默认值建设置行。
    await session.commit()
    return {
        "code": 0,
        "data": {"today": today.isoformat(), "groups": groups, "total": len(rows)},
    }


@router.post("")
async def add(
    body: AddIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    today = await _today(session, own)
    try:
        row = await followups.add(
            session,
            own.bot.id,
            category=body.category,
            title=body.title,
            notes=body.notes,
            review_on=body.review_on,
            due_on=body.due_on,
        )
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.patch("/{label}")
async def patch(
    label: str,
    body: PatchIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    changes = body.model_dump(include=body.model_fields_set)
    if not changes:
        raise refuse("nothing_to_change", ERRORS)
    if any(key in changes and changes[key] is None for key in ("category", "title")):
        raise refuse("null_not_allowed", ERRORS)
    today = await _today(session, own)
    try:
        row = await followups.update(
            session,
            own.bot.id,
            seq,
            category=changes.get("category"),
            title=changes.get("title"),
            notes=changes.get("notes", followups.KEEP),
            review_on=changes.get("review_on", followups.KEEP),
            due_on=changes.get("due_on", followups.KEEP),
        )
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/{label}/close")
async def close(
    label: str,
    body: CloseIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    today = await _today(session, own)
    try:
        if body.reason is not None:
            row = await owner_feedback.close(session, own.bot.id, seq, body.reason, body.note)
        else:
            row = await followups.close(session, own.bot.id, seq, dropped=body.dropped)
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/{label}/snooze")
async def snooze(
    label: str,
    body: SnoozeIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    seq = _seq(label)
    today = await _today(session, own)
    try:
        row = await owner_feedback.reschedule(
            session, own.bot.id, seq, body.until, body.note, today=today
        )
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/{label}/feedback")
async def feedback(
    label: str,
    body: FeedbackIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """补充一段话：写进这一条的备注，立刻交回助理重新判断。"""
    seq = _seq(label)
    today = await _today(session, own)
    try:
        row = await owner_feedback.supplement(session, own.bot.id, seq, body.text, today=today)
    except FollowupError as exc:
        await session.rollback()
        raise refuse(exc.code, ERRORS) from None
    data = _item(row, today)
    await session.commit()
    return {"code": 0, "data": data}
