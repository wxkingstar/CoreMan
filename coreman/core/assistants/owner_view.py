"""「我的助理」页的读取：本人的助理是哪一条、概览、没有助理时的开通指引。

只给所有者本人用：调用方（`/api/me/assistant` 的路由）传进来的 `bot` 一律由登录人推出。概览只有
元数据——状态、运行时的名字、是否在班、轮次、轮询健康——不读任何内容列。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, eligibility, jobs, lifecycle, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.approvals import tower
from coreman.core.config import get_settings
from coreman.core.db.models import (
    AssistantApproval,
    AssistantJob,
    AssistantLifecycle,
    AssistantSettings,
    AssistantWatch,
    Bot,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import online
from coreman.core.timeutils import utcnow

# 开通的三步，页面按这些代号显示文案：私聊助理应用 → 点卡片上的「开通」→ 完成飞书授权。
HOW_TO = ("open_private_chat", "accept_offer", "authorize")
# 「等你出手」与「最近落地」各看哪些状态的审批单。最近落地列全部终态：已执行、被拒、撤回、
# 失败、过期、内容已过时作废；已批准还没执行完的（`approved`）是途中状态，两边都不列。
PENDING = ("pending",)
SETTLED = ("executed", "rejected", "withdrawn", "failed", "expired", "stale")
APPROVAL_LIMIT = 30


async def current(session: AsyncSession, user_id: uuid.UUID) -> Bot | None:
    """这个人现在的助理记录：还启用着的那一条（部分唯一索引保证至多一条）。

    停用的最后一步才把记录置为不启用，所以开通中、停用中的记录都算；停用完成的不算。
    """
    found: Bot | None = await session.scalar(
        select(Bot)
        .where(Bot.kind == "assistant", Bot.owner_id == user_id, Bot.enabled.is_(True))
        .order_by(Bot.created_at.desc())
        .limit(1)
    )
    return found


def approval_link() -> str | None:
    """审批系统的网页入口；没配、或配的不是 http(s) 地址时不给。"""
    return tower.web_url(get_settings())


def _flow(row: AssistantLifecycle | None) -> dict[str, Any] | None:
    return None if row is None else {"action": row.action, "step": row.step}


async def overview(session: AsyncSession, bot: Bot, now: datetime) -> dict[str, Any]:
    """所有者看到的概览。会按默认值建设置行，调用方提交。"""
    prefs = await assistant_settings.get(session, bot.id)
    own = prefs.daily_turn_budget
    used = await delivery.turns_used(
        session, bot.id, assistant_settings.zone(prefs), assistant_settings.local_today(prefs, now)
    )
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    node = (
        await session.get(RuntimeNode, relay.runtime_node_id)
        if relay is not None and relay.runtime_node_id
        else None
    )
    # 只取状态、时间与错误代码三列：游标与上一轮的会话顺序不取。
    watch = (
        await session.execute(
            select(
                AssistantWatch.state, AssistantWatch.last_ok_at, AssistantWatch.last_error
            ).where(AssistantWatch.bot_id == bot.id)
        )
    ).first()
    flow = await lifecycle.live(session, bot.owner_id, lock=False) if bot.owner_id else None
    name = (get_settings().tower_display_name or "").strip() or None
    switched = assistant_settings.override(prefs, now)
    return {
        "exists": True,
        "bot_id": str(bot.id),
        "name": bot.name,
        "state": bot.assistant_state,
        "runtime": (
            {
                "name": node.name if node is not None else relay.name,
                "online": node is not None and online(node),
            }
            if relay is not None
            else None
        ),
        # 可用状态下为什么现在跑不了轮次（运行时离线、不满足隔离……）；能跑是 None。
        "paused_reason": (
            await runtime.turn_blocker(session, bot) if bot.assistant_state == "active" else None
        ),
        # 与管理台列表同一口径：启用、开通好且此刻在班；开通中、停用中的助理不算在班。
        "on_shift": bool(
            bot.enabled
            and bot.assistant_state == "active"
            and assistant_settings.on_shift(prefs, now)
        ),
        # 全天候（默认）还是按工作时段。
        "always_on": prefs.always_on,
        # 还没过期的「上班」「下班」临时切换与它的到期时刻；没有临时切换时两项都是 None。
        "shift_override": switched,
        "shift_override_until": (
            prefs.shift_override_until.isoformat()
            if switched is not None and prefs.shift_override_until is not None
            else None
        ),
        "cold_start": {
            "state": assistant_settings.cold_start_state(prefs),
            "step": prefs.cold_start_step,
            "outcome": prefs.cold_start_outcome,
        },
        "turns": {
            "today": used,
            # 个人值与平台默认里较小的那个（`delivery.effective_budget`）。
            "budget": delivery.effective_budget(own, await delivery.default_budget(session)),
        },
        "watch": (
            {
                "state": watch[0],
                "last_ok_at": watch[1].isoformat() if watch[1] else None,
                "last_error": watch[2],
            }
            if watch is not None
            else None
        ),
        "lifecycle": _flow(flow),
        "content_locked": not await sealed.key_ready(session, bot.id),
        "approval_system": {"name": name, "url": approval_link()},
    }


async def absent(session: AsyncSession, user: User) -> dict[str, Any]:
    """没有助理时页面显示的内容：能不能开通、不能的原因代号、怎么开通。"""
    reason = await eligibility.refusal(session, user)
    app_name = (
        await session.scalar(
            select(PlatformApp.name)
            .where(
                PlatformApp.platform == "feishu",
                PlatformApp.enabled.is_(True),
                PlatformApp.capabilities.contains(["assistant"]),
                PlatformApp.app_id.is_not(None),
            )
            .order_by(PlatformApp.name)
            .limit(1)
        )
        if reason is None
        else None
    )
    flow = await lifecycle.live(session, user.id, lock=False)
    return {
        "exists": False,
        "eligible": reason is None,
        "reason": reason,
        "how_to": {"app_name": app_name, "steps": list(HOW_TO)},
        "lifecycle": _flow(flow),
    }


async def target_names(session: AsyncSession, rows: Iterable[AssistantJob]) -> dict[uuid.UUID, str]:
    """调度的目标员工现在叫什么（只取编号与名字两列）；员工已删除的不在结果里。"""
    wanted = {row.target_bot_id for row in rows if row.target_bot_id is not None}
    if not wanted:
        return {}
    found = await session.execute(select(Bot.id, Bot.name).where(Bot.id.in_(wanted)))
    return {bot_id: name for bot_id, name in found.all()}


def job(row: AssistantJob, names: Mapping[uuid.UUID, str]) -> dict[str, Any]:
    """所有者在页面上看到的一个子任务或一次调度；`row` 的内容列要已还原。

    比给助理的视图多两项：`origin`（`system` 是平台派的下班整理）与 `target`（调度的目标员工；
    子任务是 None，员工已删除时名字为 None，标题里仍带着派出当时的名字）。
    """
    out = jobs.view(row, full=True)
    out["origin"] = row.origin
    out["target"] = (
        {"id": str(row.target_bot_id), "name": names.get(row.target_bot_id)}
        if row.target_bot_id is not None
        else None
    )
    return out


async def approvals(
    session: AsyncSession,
    bot: Bot,
    viewer: uuid.UUID,
    statuses: tuple[str, ...],
    *,
    limit: int = APPROVAL_LIMIT,
) -> list[AssistantApproval]:
    """这位助理处于这些状态的审批单，新近有动静的在前。

    内容列只对所有者本人还原：`viewer` 不是所有者时摘要、理由、结果都是空的，
    `sealed.is_locked(row)` 为真。
    """
    latest = func.coalesce(
        AssistantApproval.executed_at, AssistantApproval.decided_at, AssistantApproval.created_at
    )
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .where(AssistantApproval.bot_id == bot.id, AssistantApproval.status.in_(statuses))
            .order_by(latest.desc(), AssistantApproval.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows, viewer=viewer)
    return rows


def _summary(row: AssistantApproval) -> str | None:
    """提交时平台生成的单行摘要（渠道与对象）；读不出来是 None。"""
    stored = row.args_enc if isinstance(row.args_enc, dict) else {}
    found = stored.get("abstract")
    return found if isinstance(found, str) and found else None


def pending_view(row: AssistantApproval, link: str | None) -> dict[str, Any]:
    """等所有者批的一张单：只有编号、工具名、摘要、提交时间与审批系统的入口。

    要发出的正文不在这里给：它在审批系统里，由所有者在那边看着批。
    """
    return {
        "id": str(row.id),
        "tool": row.tool,
        "summary": _summary(row),
        "created_at": row.created_at.isoformat(),
        "link": link,
    }


def settled_view(row: AssistantApproval) -> dict[str, Any]:
    """已经落地的一张单：结果、摘要、审批人写的理由与各个时刻。"""
    return {
        "id": str(row.id),
        "tool": row.tool,
        "channel": row.channel,
        "status": row.status,
        "summary": _summary(row),
        "reason": row.reason_enc or None,
        "error": row.error_code,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "locked": sealed.is_locked(row),
    }


async def preferences(
    session: AsyncSession, row: AssistantSettings, now: datetime
) -> dict[str, Any]:
    """页面上的设置表单要的全部：`settings.view` 加上每日预算、平台默认值与各项上限。

    `row` 的内容列要已还原（`settings.get` 取出来的就是）。
    """
    default = await delivery.default_budget(session)
    return {
        **assistant_settings.view(row, now),
        # 个人值；None 表示跟随平台默认。
        "daily_turn_budget": row.daily_turn_budget,
        "platform_daily_turn_budget": default,
        # 实际生效的：两者里较小的。管理员把平台默认调到个人值以下时，个人值留着但不再生效。
        "effective_daily_turn_budget": delivery.effective_budget(row.daily_turn_budget, default),
        "limits": {
            "focus_chats": assistant_settings.MAX_FOCUS,
            "ignored_chats": assistant_settings.MAX_IGNORED,
            "budget_min": delivery.BUDGET_MIN,
        },
        "locked": sealed.is_locked(row),
    }


async def set_budget(
    session: AsyncSession, bot_id: uuid.UUID, value: int | None
) -> AssistantSettings:
    """所有者改自己的每日轮次预算；`None` 是清掉个人值、跟随平台默认。只 flush，调用方提交。

    个人只能调低：调到平台默认值之上，管理员的设置就形同虚设（实际生效的本来也是两者里
    较小的那个，见 `delivery.effective_budget`；这里拒绝是为了不让页面存下一个不生效的数）。
    范围下限与上限由入参模型挡。

    Raises:
        SettingsError: budget_over_platform
    """
    limit = await delivery.default_budget(session)
    if value is not None and value > limit:
        raise assistant_settings.SettingsError("budget_over_platform", limit=limit)
    row = await assistant_settings.get(session, bot_id)
    row.daily_turn_budget = value
    row.updated_at = utcnow()
    await session.flush()
    return row


SHIFT_STATES = ("on", "off", "auto")


def switch_shift(row: AssistantSettings, state: str, now: datetime) -> None:
    """页面上的「上班」「下班」「按工作时段」（全天候时是「恢复全天候」）：与私聊里说「上班」
    「下班」同一个效果。

    `on` / `off` 的到期时刻照 `settings.clock_in` / `clock_out`（全天候时 `on` 只清掉「下班」）；
    `auto` 清掉临时切换。上班、或下班时本来在班，都记下今天上过班（每日整理凭它触发；
    前一天还欠着整理时不覆盖，见 `settings.mark_worked`）。
    调用方加锁、提交。

    Raises:
        ValueError: 不认识的状态
    """
    if state not in SHIFT_STATES:
        raise ValueError(f"unknown shift state: {state}")
    if state == "auto":
        assistant_settings.clear_override(row)
        return
    if state == "on" or assistant_settings.on_shift(row, now):
        assistant_settings.mark_worked(row, now)
    if state == "on":
        assistant_settings.clock_in(row, now)
    else:
        assistant_settings.clock_out(row, now)
