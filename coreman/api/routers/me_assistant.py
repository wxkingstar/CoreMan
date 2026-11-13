"""「我的助理」页的接口：概览、等你出手、子任务、最近落地、设置、停用。

只对所有者本人：要求交互登录会话（机器人令牌 403），助理由登录人推出。读取密文的接口在返回前
记一次访问（`access_log.record`）。待跟进与知识页在 `me_assistant_followups.py`、
`me_assistant_notes.py`。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import assistant_deprovision
from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.me_assistant_common import (
    DEPENDENCIES,
    PREFIX,
    Mine,
    mine,
    refuse,
    writable,
)
from coreman.core.assistants import (
    access_log,
    cold_start,
    delivery,
    followups,
    jobs,
    owner_view,
    sealed,
)
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantSettings, User
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)

# 「最近结束」看最近这么多个子任务里已经结束的。
JOB_LIMIT = 20
JOB_ERRORS: dict[str, tuple[int, str]] = {
    "job_not_found": (404, "这个子任务不存在"),
    "job_closed": (409, "这个子任务已经结束"),
}


SETTINGS_ERRORS: dict[str, tuple[int, str]] = {
    "nothing_to_update": (422, "至少修改一项"),
    "invalid_timezone": (422, "不认识的时区，请用 IANA 时区名，例如 Asia/Shanghai"),
    "invalid_work_hours": (
        422,
        "时间要写成 HH:MM，且上班时间必须早于下班时间（不支持跨午夜的时段）",
    ),
    "invalid_workdays": (422, "工作日是 1（周一）到 7（周日），至少选一个，不能重复"),
    "invalid_chats": (422, "会话要填 chat_id（以 oc_ 开头）"),
    "too_many_chats": (
        422,
        f"重点群最多 {assistant_settings.MAX_FOCUS} 个，"
        f"忽略的会话最多 {assistant_settings.MAX_IGNORED} 个",
    ),
    "chat_in_both_lists": (422, "同一个会话不能既是重点群又被忽略"),
    "budget_over_platform": (422, "每日轮次预算不能超过平台默认值"),
    "content_locked": (409, "加密保存的个人内容已不可读取，不能再写入重点群与忽略的会话"),
}
_ChatId = Annotated[str, StringConstraints(max_length=100)]


class SettingsIn(BaseModel):
    """所有者改的设置。这里只挡类型与离谱的长度：取值规则由 `settings.update` 判断并给出原因代码。

    前六项给 null 等同于没给；`daily_turn_budget` 给 null 是清掉个人值、跟随平台默认。
    """

    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, max_length=64)
    work_start: str | None = Field(default=None, max_length=5)
    work_end: str | None = Field(default=None, max_length=5)
    workdays: list[StrictInt] | None = Field(default=None, max_length=7)
    focus_chats: list[_ChatId] | None = Field(default=None, max_length=200)
    ignored_chats: list[_ChatId] | None = Field(default=None, max_length=200)
    daily_turn_budget: StrictInt | None = Field(
        default=None, ge=delivery.BUDGET_MIN, le=delivery.BUDGET_MAX
    )


VOICE_ERRORS: dict[str, tuple[int, str]] = {
    "cold_start_unfinished": (409, "冷启动还没做完：口吻页由它生成，做完或跳过之后才能重新生成"),
    "content_locked": (409, "加密保存的个人内容已不可读取"),
    "voice_page_edited": (
        409,
        "口吻页是你自己写的，重新生成不会覆盖它；想让助理重写，先删掉这一页",
    ),
    "voice_daily_limit": (
        429,
        f"今天已经重新生成过 {cold_start.REGENERATE_DAILY} 次了，明天再试",
    ),
    "authorization_required": (409, "需要先在私聊里重新完成飞书授权"),
    "feishu_unavailable": (502, "暂时读不了飞书，稍后再试"),
    "too_few_samples": (409, "最近 7 天里你自己发出的消息太少，归纳不出口吻"),
    "assistant_unavailable": (409, "个人助理现在跑不了这一轮，稍后再试"),
}


class DeprovisionIn(BaseModel):
    """停用不可撤销：必须明确带上 `confirm: true`（JSON 的 true；"true"、1 都不算）。"""

    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


class ShiftIn(BaseModel):
    """`on` 上班、`off` 下班、`auto` 回到按工作时段。"""

    model_config = ConfigDict(extra="forbid")
    state: Literal["on", "off", "auto"]


@router.get("")
async def overview(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """概览；没有助理时回空状态与开通指引（不是 404：页面要据此显示怎么开通）。"""
    bot = await owner_view.current(session, actor.id)
    data = (
        await owner_view.absent(session, actor)
        if bot is None
        else await owner_view.overview(session, bot, utcnow())
    )
    # 概览会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/jobs")
async def job_list(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """正在跑与最近结束的子任务与调度，含交代原文、轮数与结论。

    调度公共员工与子任务同表：`kind` 是 `bot_help` 的带目标员工的名字（`target`）；`origin` 是
    `system` 的是平台派的（下班整理），页面上与所有者让助理派的分开显示。
    """
    active = await jobs.list_(session, own.bot.id, scope="active", limit=JOB_LIMIT)
    recent = [
        row
        for row in await jobs.list_(session, own.bot.id, scope="recent", limit=JOB_LIMIT)
        if row.status in jobs.ENDED
    ]
    # 核心函数已经还原过；这里按查看者再核一次，不是所有者本人就全部收回。
    await sealed.open_rows(session, [*active, *recent], viewer=own.actor.id)
    names = await owner_view.target_names(session, [*active, *recent])
    await access_log.record(session, own.bot.id, own.actor.id, "jobs.read", "list")
    data = {
        "active": [owner_view.job(row, names) for row in active],
        "recent": [owner_view.job(row, names) for row in recent],
    }
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(
    job_id: uuid.UUID,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者取消一个进行中的子任务或调度；`report=True` 让助理在主会话里得知这件事。"""
    try:
        row = await jobs.cancel(session, own.bot.id, job_id, code="cancelled_by_owner", report=True)
    except jobs.JobError as exc:
        await session.rollback()
        raise refuse(exc.code, JOB_ERRORS) from None
    data = {"id": str(row.id), "status": row.status}
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/attention")
async def attention(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """等你出手：待批的审批单、被卡住的子任务、今天到期的待跟进。"""
    bot, actor = own.bot, own.actor
    prefs = await assistant_settings.get(session, bot.id)
    today = assistant_settings.local_today(prefs, utcnow())
    link = owner_view.approval_link()
    pending = await owner_view.approvals(session, bot, actor.id, owner_view.PENDING)
    blocked = await jobs.list_(session, bot.id, scope="blocked", limit=JOB_LIMIT)
    due = await followups.due(session, bot.id, today)
    # 核心函数已经还原过；这里按查看者再核一次。
    await sealed.open_rows(session, [*blocked, *due], viewer=actor.id)
    names = await owner_view.target_names(session, blocked)
    await access_log.record(session, bot.id, actor.id, "approvals.read", "pending")
    await access_log.record(session, bot.id, actor.id, "jobs.read", "blocked")
    await access_log.record(session, bot.id, actor.id, "followups.read", "today")
    data = {
        "today": today.isoformat(),
        "approvals": [owner_view.pending_view(row, link) for row in pending],
        "jobs": [owner_view.job(row, names) for row in blocked],
        "followups": [followups.view(row) for row in due],
    }
    # 取今天到期的条目会把到了回看日的推后条目改回未结。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/approvals/recent")
async def recent_approvals(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """最近落地：全部终态的审批单（已执行、被拒、撤回、失败、过期、作废），给本人解密后的摘要与理由。"""
    rows = await owner_view.approvals(session, own.bot, own.actor.id, owner_view.SETTLED)
    await access_log.record(session, own.bot.id, own.actor.id, "approvals.read", "recent")
    data = {"items": [owner_view.settled_view(row) for row in rows]}
    # 访问记录与这次读取同一个事务：提交不了，内容也不交出去。
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/settings")
async def settings_view(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    now = utcnow()
    row = await assistant_settings.get(session, own.bot.id)
    # 重点群与忽略的会话是密文：只对所有者本人还原。
    await sealed.open_row(session, row, viewer=own.actor.id)
    await access_log.record(session, own.bot.id, own.actor.id, "settings.read", "")
    data = await owner_view.preferences(session, row, now)
    # 第一次读会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}


@router.patch("/settings")
async def settings_patch(
    body: SettingsIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    schedule = body.model_dump(exclude={"daily_turn_budget"}, exclude_none=True)
    # None 对预算是合法取值，不能用它判断「没传」。
    budget_given = "daily_turn_budget" in body.model_fields_set
    try:
        if not schedule and not budget_given:
            raise assistant_settings.SettingsError("nothing_to_update")
        if schedule:
            await assistant_settings.update(session, own.bot.id, now=now, **schedule)
        if budget_given:
            await owner_view.set_budget(session, own.bot.id, body.daily_turn_budget)
    except assistant_settings.SettingsError as exc:
        # 一项不过，这次请求里已经做了的改动一并回滚。
        await session.rollback()
        raise refuse(exc.code, SETTINGS_ERRORS) from None
    row = await assistant_settings.get(session, own.bot.id)
    data = await owner_view.preferences(session, row, now)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/shift")
async def shift(
    body: ShiftIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """即时上班、下班，或回到按工作时段；与私聊里说「上班」「下班」同一个效果。返回设置。"""
    now = utcnow()
    await assistant_settings.get(session, own.bot.id)
    await session.flush()
    row = await session.get(
        AssistantSettings, own.bot.id, with_for_update=True, populate_existing=True
    )
    assert row is not None
    owner_view.switch_shift(row, body.state, now)
    await sealed.open_row(session, row, viewer=own.actor.id)
    # 响应里带着解密后的设置（重点群、忽略的会话）：与读设置一样记一次。
    await access_log.record(session, own.bot.id, own.actor.id, "settings.read", "shift")
    data = await owner_view.preferences(session, row, now)
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/voice/regenerate")
async def voice_regenerate(
    request: Request, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """重新生成口吻页：再读一次所有者最近自己发出的消息，排一轮不出声的归纳。

    冷启动没做完时拒绝；每天至多 `cold_start.REGENERATE_DAILY` 次。这个请求里要以所有者身份
    读飞书（几个会话的近期消息），可能要十几秒；读到的消息不出现在响应里。
    """
    try:
        _task, left = await cold_start.regenerate_voice(
            session, request.app.state.cipher, own.bot, utcnow()
        )
    except cold_start.VoiceError as exc:
        # 提交而不是回滚：读飞书时可能刚换过所有者的令牌（刷新令牌是一次性的），回滚会把新令牌
        # 丢掉。到这里为止这个事务里没有别的改动。
        await session.commit()
        raise refuse(exc.code, VOICE_ERRORS) from None
    await session.commit()
    return {"code": 0, "data": {"queued": True, "left_today": left}}


@router.post("/deprovision")
async def deprovision(
    body: DeprovisionIn,
    request: Request,
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者停用自己的助理：与私聊里发「停用助理」并确认是同一件事，一旦开始不可撤销。

    不要求助理处于可用状态：开通到一半、已经在停用中都可以点（后者不重复做）。
    """
    if body.confirm is not True:
        raise ApiError(422, 422, "停用不可撤销，请带上 confirm: true")
    state = await assistant_deprovision.begin(
        session, request, own.bot, trigger="owner", actor=own.actor
    )
    await session.commit()
    return {"code": 0, "data": {"state": state}}
