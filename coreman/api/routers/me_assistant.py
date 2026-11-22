"""「我的助理」页的接口：概览、总表、子任务、最近落地、设置、停用。

只对所有者本人：要求交互登录会话（机器人令牌 403），助理由登录人推出。读取密文的接口在返回前
记一次访问（`access_log.record`）。待跟进与知识页在 `me_assistant_followups.py`、
`me_assistant_notes.py`。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
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
from coreman.api.system_grants import apply_grants, current_grants, lock_bot
from coreman.core.assistants import (
    access_log,
    access_requests,
    board,
    capabilities,
    chat_choices,
    cold_start,
    delivery,
    followups,
    gaps,
    jobs,
    message_range,
    owner_view,
    reset,
    sealed,
    voice,
    watch,
)
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.records import is_assistant
from coreman.core.db.models import AssistantApproval, AssistantSettings, BusinessSystem, User
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.feishu_personal.toolbase import Call
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
        f"不托管的会话最多 {assistant_settings.MAX_IGNORED} 个，"
        f"只托管的会话最多 {assistant_settings.MAX_MANAGED} 个",
    ),
    "chat_in_both_lists": (422, "同一个会话不能既是重点群又不托管"),
    "invalid_chat_scope": (422, "托管范围只能是「全部会话」或「只托管名单里的会话」"),
    "focus_not_managed": (422, "只托管名单里的会话时，重点群必须在托管名单里"),
    "invalid_digest_times": (
        422,
        f"摘要时点要写成 HH:MM，1 到 {assistant_settings.MAX_DIGEST_TIMES} 个，不能重复",
    ),
    "invalid_quiet_hours": (422, "免打扰时段要写成 HH:MM，开始与结束不能相同"),
    "budget_over_platform": (422, "每日轮次预算不能超过平台默认值"),
    "content_locked": (409, "加密保存的个人内容已不可读取，不能再写入重点群与托管名单"),
}
CHATS_ERRORS: dict[str, tuple[int, str]] = {
    "assistant_unavailable": (409, "个人助理现在不可用，读不了你的会话列表"),
    "authorization_required": (409, "需要先在私聊里重新完成飞书授权"),
    "feishu_busy": (429, "飞书接口现在用量紧张，稍后再试"),
    "feishu_unavailable": (502, "暂时读不了你的会话列表，稍后再试"),
}
_ChatId = Annotated[str, StringConstraints(max_length=100)]


class SettingsIn(BaseModel):
    """所有者改的设置。这里只挡类型与离谱的长度：取值规则由 `settings.update` 判断并给出原因代码。

    除 `daily_turn_budget` 外各项给 null 等同于没给；`daily_turn_budget` 给 null 是清掉个人值、
    跟随平台默认。
    """

    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, max_length=64)
    always_on: StrictBool | None = None
    work_start: str | None = Field(default=None, max_length=5)
    work_end: str | None = Field(default=None, max_length=5)
    workdays: list[StrictInt] | None = Field(default=None, max_length=7)
    focus_chats: list[_ChatId] | None = Field(default=None, max_length=200)
    ignored_chats: list[_ChatId] | None = Field(default=None, max_length=400)
    managed_chats: list[_ChatId] | None = Field(default=None, max_length=400)
    chat_scope: Literal["all", "listed"] | None = None
    digest_times: list[Annotated[str, StringConstraints(max_length=5)]] | None = Field(
        default=None, max_length=24
    )
    quiet_enabled: StrictBool | None = None
    quiet_start: str | None = Field(default=None, max_length=5)
    quiet_end: str | None = Field(default=None, max_length=5)
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
    "voice_attempt_limit": (
        429,
        f"试得太频繁了（每小时至多 {cold_start.REGENERATE_HOURLY} 次、"
        f"每天至多 {cold_start.REGENERATE_ATTEMPTS_DAILY} 次），过一阵再试",
    ),
    "regenerate_in_progress": (409, "上一次重新生成还在读取飞书，等它结束再试"),
    "feishu_busy": (429, "飞书接口现在用量紧张，稍后再试"),
    "authorization_required": (409, "需要先在私聊里重新完成飞书授权"),
    "feishu_unavailable": (502, "暂时读不了飞书，稍后再试"),
    "too_few_samples": (
        409,
        f"最近 {voice.WINDOW.days} 天里你自己发出的消息太少，归纳不出口吻",
    ),
    "assistant_unavailable": (409, "个人助理现在跑不了这一轮，稍后再试"),
}


class ResetIn(BaseModel):
    """清掉的事项找不回来：必须明确带上 `confirm: true`。"""

    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


class DeprovisionIn(BaseModel):
    """停用不可撤销：必须明确带上 `confirm: true`（JSON 的 true；"true"、1 都不算）。"""

    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


RANGE_ERRORS: dict[str, tuple[int, str]] = {
    "authorization_required": (409, "还没有完成飞书授权，先在私聊里完成授权"),
    "assistant_inactive": (409, "个人助理现在不可用"),
    "app_message_permission_missing": (409, "助理应用缺少读消息的权限，请联系管理员"),
    "app_send_permission_missing": (409, "助理应用缺少以本人身份发消息的权限，请联系管理员"),
    "app_scope_discovery_permission_missing": (409, "助理应用缺少查询自身权限的能力，请联系管理员"),
    "feishu_busy": (429, "飞书接口现在用量紧张，稍后再试"),
}


class RangeIn(BaseModel):
    """授权的消息范围：全部会话、只群聊、只单聊。"""

    model_config = ConfigDict(extra="forbid")
    message_range: Literal["all", "group", "p2p"]


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


GAP_ERRORS: dict[str, tuple[int, str]] = {
    "gap_not_found": (404, "这个缺口不存在"),
    "gap_resolved": (409, "这个缺口已经可以用了"),
    "not_business_system": (409, "只有业务系统的缺口能在这里开通或申请"),
    "owner_can_grant": (409, "这个系统对你的助理开放，直接点「开通」即可"),
    "system_not_requestable": (409, "这个系统没有登记或已停用，请联系平台管理员"),
    "already_pending": (409, "已经有一张在途的申请"),
    "gap_not_open": (409, "这个缺口现在不能开通或申请"),
    "request_not_found": (404, "没有在途的申请"),
    "request_decided": (409, "这张申请已经处理过了"),
    "invalid_reason": (422, f"理由写一两句话，不超过 {access_requests.MAX_REASON} 字"),
    "assistant_not_active": (409, "个人助理现在不可用"),
    "needs_request": (
        409,
        "这个系统没有对你的助理开放，不能直接开通：请点「申请」，由平台管理员审批",
    ),
    "grant_not_enough": (
        409,
        "开通这个系统也解决不了这个缺口（它要的级别这个系统给不了），请联系平台管理员",
    ),
}


class RequestIn(BaseModel):
    """理由是所有者自己写、给平台管理员看的明文；长度与空白由 `access_requests` 判断。
    只读还是读写不由页面定：按缺口算（`gaps.need_write`）。"""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=access_requests.MAX_REASON * 4)


@router.post("/gaps/{label}/decline")
async def gap_decline(
    label: str, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """所有者明确不开这个缺口：不再进总表，助理得知这条路走不通。"""
    try:
        row = await gaps.decline(session, own.bot.id, gaps.parse_label(label))
    except gaps.GapError as exc:
        await session.rollback()
        raise refuse(exc.code, GAP_ERRORS) from None
    data = {"id": gaps.label(row.seq), "status": row.status}
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/gaps/{label}/grant")
async def gap_grant(
    label: str,
    request: Request,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """对这位助理开放的业务系统：所有者是它的机器人管理员，直接在现有授权上加这一个。

    级别按缺口算：报的时候已经能读的要写（`gaps.need_write`）。开通后级别升不上去（解决不了
    这个缺口）的拒绝，不写授权、不记审计。`apply_grants` 自己不查权限：这里只认登录人本人的、
    启用中的个人助理，且系统白名单已经含它（`fix_for` 是 self_grant，`apply_grants` 锁住系统行
    后再核一次）；白名单不在这里改。缺口已经提交了申请（之后管理员直接把它加进了白名单）的，
    在途的申请随之撤回。
    """
    bot = own.bot
    if not bot.enabled or not is_assistant(bot) or bot.owner_id != own.actor.id:
        raise refuse("assistant_not_active", GAP_ERRORS)
    try:
        seq = gaps.parse_label(label)
        # 加锁顺序与 access_requests 相同：咨询锁 → 申请行 → 缺口行 → 机器人行 → 系统行。
        await gaps.lock(session, bot.id)
        gap = await gaps.get(session, bot.id, seq)
        if gap.kind != "business_system":
            raise access_requests.RequestError("not_business_system")
        if gap.status not in gaps.LIVE:
            raise access_requests.RequestError("gap_not_open")
        fix = await gaps.fix_for(session, bot.id, gap)
        if fix != "self_grant":
            raise access_requests.RequestError(
                "needs_request" if fix == "request" else "system_not_requestable"
            )
        write = gaps.need_write(gap)
        system = await session.get(BusinessSystem, gap.key)
        assert system is not None  # fix_for 已确认登记过
        if capabilities.grant_rank(system, write) <= gap.seen_rank:
            raise access_requests.RequestError("grant_not_enough")
        if gap.status == "requested":
            await access_requests.withdraw(session, bot.id, seq)
        gap = await gaps.get(session, bot.id, seq, lock=True)
    except (gaps.GapError, access_requests.RequestError) as exc:
        await session.rollback()
        raise refuse(exc.code, GAP_ERRORS) from None
    # 先锁机器人行再读现有授权：与管理台改权限互相排队，不会覆盖掉对方刚写的。
    await lock_bot(session, bot)
    keys, writes = await current_grants(session, bot.id)
    await apply_grants(
        session,
        request,
        own.actor,
        bot,
        keys=[*keys, gap.key],
        write_keys=[*writes, gap.key] if write else writes,
        comment=f"所有者在「我的助理」页开通缺口 {gaps.label(gap.seq)}",
    )
    await session.refresh(gap, ["status"])
    data = {"id": gaps.label(gap.seq), "status": gap.status}
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/gaps/{label}/request")
async def gap_request(
    label: str,
    body: RequestIn,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """白名单外的业务系统：所有者确认理由后提交，平台管理员审批。理由会给管理员看。"""
    bot = own.bot
    if not bot.enabled or not is_assistant(bot) or bot.owner_id != own.actor.id:
        raise refuse("assistant_not_active", GAP_ERRORS)
    try:
        seq = gaps.parse_label(label)
        await gaps.lock(session, bot.id)
        write = gaps.need_write(await gaps.get(session, bot.id, seq))
        row = await access_requests.open_request(
            session, bot, seq, write=write, reason=body.reason, actor=own.actor
        )
    except (gaps.GapError, access_requests.RequestError) as exc:
        await session.rollback()
        raise refuse(exc.code, GAP_ERRORS) from None
    data = {"id": gaps.label(seq), "status": "requested", "request_id": str(row.id)}
    await session.commit()
    return {"code": 0, "data": data}


@router.post("/gaps/{label}/withdraw")
async def gap_withdraw(
    label: str, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """所有者撤回这个缺口上在途的申请，缺口回到待开通。"""
    try:
        seq = gaps.parse_label(label)
        await access_requests.withdraw(session, own.bot.id, seq)
    except (gaps.GapError, access_requests.RequestError) as exc:
        await session.rollback()
        raise refuse(exc.code, GAP_ERRORS) from None
    data = {"id": gaps.label(seq), "status": "open"}
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/board")
async def board_view(
    own: Mine = Depends(mine), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """总表：所有未结的待跟进按状态分组（等你、执行中、等别人、排期、停滞），加上在途审批单。

    审批单与卡片同一口径：一轮里合成的一张单只算一张、只列一行（`items` 是它含几项），早的在前，
    至多 `board.APPROVALS_MAX` 行；`approvals_total` 是总张数。
    """
    bot, actor = own.bot, own.actor
    prefs = await assistant_settings.get(session, bot.id)
    result = await board.build(session, bot.id, prefs, utcnow())
    all_items = [item for items in result.groups.values() for item in items]
    rows = [item.row for item in all_items]
    linked = [item.job for item in all_items if item.job]
    held = [a for item in all_items for a in item.approvals]
    # 核心函数已经还原过；这里按查看者再核一次。
    gap_rows = [entry.row for entry in result.gaps]
    await sealed.open_rows(
        session, [*rows, *linked, *result.approvals, *held, *gap_rows], viewer=actor.id
    )
    gap_labels = {entry.row.id: gaps.label(entry.row.seq) for entry in result.gaps}
    names = await owner_view.target_names(session, linked)
    link = owner_view.approval_link()
    latest = await access_requests.latest_for(session, [entry.row.id for entry in result.gaps])

    def gap_view(entry: board.Gap) -> dict[str, Any]:
        out = gaps.view(entry.row, entry.fix, entry.followups)
        out["stage"] = entry.stage
        found = latest.get(entry.row.id)
        out["request"] = (
            {
                "status": found.status,
                "reason": found.reason,
                "decision_note": found.decision_note,
                "created_at": found.created_at.isoformat(),
            }
            if found is not None
            else None
        )
        return out

    def approval(row: AssistantApproval) -> dict[str, Any]:
        out = owner_view.pending_view(row, link)
        out["items"] = result.approval_items.get(row.id, 1)
        return out

    def item(entry: board.Item) -> dict[str, Any]:
        out = followups.view(entry.row, notes=False)
        out.update(
            stage=entry.stage,
            reason=entry.reason,
            idle_days=entry.idle_days,
            suggestion=board.suggestion(entry.row),
            need=board.need(entry.row),
            job=owner_view.job(entry.job, names) if entry.job else None,
            gap=gap_labels.get(entry.row.gap_id) if entry.row.gap_id else None,
            approvals=[approval(a) for a in entry.approvals],
        )
        return out

    await access_log.record(session, bot.id, actor.id, "approvals.read", "pending")
    await access_log.record(session, bot.id, actor.id, "followups.read", "board")
    await access_log.record(session, bot.id, actor.id, "jobs.read", "board")
    data = {
        "today": result.today.isoformat(),
        "counts": result.counts(),
        "approvals": [approval(row) for row in result.approvals],
        "approvals_total": result.approvals_total,
        "groups": {name: [item(e) for e in entries] for name, entries in result.groups.items()},
        "bot_id": str(bot.id),
        "gaps": [gap_view(entry) for entry in result.gaps],
    }
    # 读取会把到了回看日的推后条目改回未结；访问记录与读取同一个事务。
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
    # 响应里带着解密后的设置（重点群、忽略的会话，不只是这次改的那几项）：与读设置一样记一次。
    await access_log.record(session, own.bot.id, own.actor.id, "settings.read", "update")
    data = await owner_view.preferences(session, row, now)
    await session.commit()
    return {"code": 0, "data": data}


@router.get("/settings/chats")
async def settings_chats(
    request: Request,
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = Query(""),
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """挑托管范围用的会话清单：最近活跃的会话，给了 `query` 时按名字或成员搜。

    以所有者身份调飞书的会话列表，只回编号、名字与类型，不读消息；不托管的会话也列出来，
    好让他勾回去。
    """
    prefs = await assistant_settings.get(session, own.bot.id)
    scope, _code = await watch.owner_scope(session, own.bot, prefs)
    if scope is None or own.bot.assistant_state != "active":
        await session.rollback()
        code = "authorization_required" if scope is None else "assistant_unavailable"
        raise refuse(code, CHATS_ERRORS)
    try:
        found = await chat_choices.listing(
            Call(session, request.app.state.cipher, scope),
            own_chat=prefs.chat_id,
            owner_open_id=prefs.open_id,
            query=query,
        )
    except ratelimit.RateLimited:
        await session.commit()
        raise refuse("feishu_busy", CHATS_ERRORS) from None
    except PersonalError as exc:
        # 刷新过的令牌已经写在授权行上，要落库。
        await session.commit()
        code = exc.code if exc.code in CHATS_ERRORS else "feishu_unavailable"
        raise refuse(code, CHATS_ERRORS) from None
    await session.commit()
    return {"code": 0, "data": {"items": [choice.payload() for choice in found]}}


@router.post("/message-range")
async def change_message_range(
    body: RangeIn,
    request: Request,
    own: Mine = Depends(writable),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """改授权的消息范围并重新授权：旧授权当场作废，新链接同时发到私聊。返回授权链接。"""
    try:
        url = await message_range.change(
            session, request.app.state.cipher, own.bot, body.message_range
        )
    except ratelimit.RateLimited:
        await session.rollback()
        raise refuse("feishu_busy", RANGE_ERRORS) from None
    except PersonalError as exc:
        await session.rollback()
        raise refuse(exc.code, RANGE_ERRORS) from None
    await session.commit()
    return {"code": 0, "data": {"authorization_url": url, "message_range": body.message_range}}


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


@router.post("/reset")
async def start_fresh(
    body: ResetIn, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """从现在开始：关掉未结的待跟进、取消子任务、撤回待批的审批单、作废还没交回的消息；
    历史对话与知识页留着做背景。返回各清了多少。"""
    if body.confirm is not True:
        raise ApiError(422, 422, "清掉的事项找不回来，请带上 confirm: true")
    cleared = await reset.start_fresh(session, own.bot.id, utcnow())
    await session.commit()
    return {"code": 0, "data": cleared.payload()}


@router.post("/voice/regenerate")
async def voice_regenerate(
    request: Request, own: Mine = Depends(writable), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """重新生成口吻页：再读一次所有者最近自己发出的消息，排一轮不出声的归纳。

    冷启动没做完时拒绝；每天至多排 `cold_start.REGENERATE_DAILY` 轮，另按尝试次数限频（读了
    飞书就算，不管排没排上）。这个请求里要以所有者身份读飞书（几个会话的近期消息），可能要
    十几秒；读到的消息不出现在响应里。读飞书不在这个请求的事务里做：先把它结束掉，核心函数
    自己分段开短事务（见 `cold_start.regenerate_voice`）。
    """
    bot_id = own.bot.id
    await session.rollback()
    try:
        _task, left = await cold_start.regenerate_voice(
            request.app.state.session_factory, request.app.state.cipher, bot_id, utcnow()
        )
    except cold_start.VoiceError as exc:
        raise refuse(exc.code, VOICE_ERRORS) from None
    return {"code": 0, "data": {"queued": True, "left_today": left}}


@router.get("/access-log")
async def access_entries(
    limit: int = Query(50, ge=1, le=access_log.MAX_LIST),
    before: int | None = Query(None, ge=1),
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """所有者看自己的访问记录：谁、什么时候、做了什么，新的在前。

    记录里只有编号、动作代码与时间，没有任何内容：看它不算一次解密，所以这个接口自己不记。
    """
    rows = await access_log.list_(session, own.bot.id, limit=limit, before=before)
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    "id": row.id,
                    "at": row.at.isoformat(),
                    "actor_kind": row.actor_kind,
                    "actor_name": row.actor_name,
                    "action": row.action,
                    "target": row.target,
                }
                for row in rows
            ],
            # 这一页装满了才可能还有更早的。
            "next_before": rows[-1].id if len(rows) == limit else None,
        },
    }


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


@router.post("/purge")
async def purge(
    body: DeprovisionIn,
    request: Request,
    own: Mine = Depends(mine),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """彻底清空：先像停用一样撤销授权、撤回待批、销毁密钥，最后把助理记录连同知识库、待跟进、
    对话记录、设置等全部数据删掉，像从没开过一样；想再用就重新开通。一旦开始不可撤销。

    停用已经在进行时点它，就在那次停用的最后一步一并删掉。
    """
    if body.confirm is not True:
        raise ApiError(422, 422, "彻底清空不可撤销，请带上 confirm: true")
    state = await assistant_deprovision.begin(
        session, request, own.bot, trigger="owner", actor=own.actor, purge=True
    )
    await session.commit()
    return {"code": 0, "data": {"state": state}}
