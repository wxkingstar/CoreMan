"""平台管理员看个人助理：谁开通了、在哪个运行时、开通与停用走到哪一步；可以停用某人的助理。

只出元数据（人、实例、节点、状态、时间、错误代码、盯消息的健康状态、今天的轮次与预算、待交回的
条目数、冷启动走到哪一步、按设置此刻是否在班、进行中的子任务数、最近一次非所有者访问个人
内容的时间），不碰任何内容列，也不解密任何东西。停用只回「已开始停用」，不回记录本身。开通范围与
可选 AI 在平台设置里改（`assistant_eligibility`、`assistant_backends`）。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import assistant_deprovision
from coreman.api.deps import get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.assistants import access_log, delivery, jobs
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import (
    AssistantInbox,
    AssistantLifecycle,
    AssistantRetiredDir,
    AssistantSettings,
    AssistantWatch,
    Bot,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import online
from coreman.core.timeutils import utcnow

_require_platform_admin = require_roles("platform_admin")
router = APIRouter(
    prefix="/api/admin/assistants",
    tags=["assistants"],
    dependencies=[Depends(verify_csrf), Depends(_require_platform_admin)],
)


class DeprovisionIn(BaseModel):
    """停用不可撤销：必须明确带上 `confirm: true`（JSON 的 true；"true"、1 都不算）。"""

    confirm: StrictBool


@router.get("")
async def list_assistants(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                Bot.id,
                Bot.assistant_state,
                Bot.enabled,
                Bot.created_at,
                User.id,
                User.login_name,
                User.display_name,
                RelayServer.id,
                RelayServer.name,
                RuntimeNode,
            )
            .join(User, User.id == Bot.owner_id)
            .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
            .outerjoin(RuntimeNode, RuntimeNode.id == RelayServer.runtime_node_id)
            .where(Bot.kind == "assistant")
            .order_by(Bot.created_at.desc())
            .limit(2000)
        )
    ).all()
    live = {
        row.bot_id: row
        for row in await session.scalars(
            select(AssistantLifecycle).where(
                AssistantLifecycle.finished_at.is_(None), AssistantLifecycle.bot_id.is_not(None)
            )
        )
    }
    counted = await session.execute(
        select(AssistantRetiredDir.bot_id, func.count()).group_by(AssistantRetiredDir.bot_id)
    )
    pending_dirs: dict[uuid.UUID, int] = {bot: count for bot, count in counted.all()}
    # 盯消息的健康状态：只取状态、时间与错误代码三列，游标与会话顺序不取。
    watched = await session.execute(
        select(
            AssistantWatch.bot_id,
            AssistantWatch.state,
            AssistantWatch.last_ok_at,
            AssistantWatch.last_error,
        )
    )
    health: dict[uuid.UUID, dict[str, Any]] = {
        bot: {
            "state": state,
            "last_ok_at": seen.isoformat() if seen else None,
            "last_error": error,
        }
        for bot, state, seen, error in watched.all()
    }
    # 今天的轮次与预算（各自所有者的时区）；收件箱里还没交回的条目数。只数行，不取内容。
    spent = await delivery.usage(session, utcnow())
    boxed = await session.execute(
        select(AssistantInbox.bot_id, func.count(AssistantInbox.id))
        .where(AssistantInbox.status == "pending")
        .group_by(AssistantInbox.bot_id)
    )
    waiting: dict[uuid.UUID, int] = {bot: int(count) for bot, count in boxed.all()}
    # 冷启动进度与是否在班：只取明文的设置列，重点群与忽略的会话（加密列）不取。
    prefs = await session.execute(
        select(
            AssistantSettings.bot_id,
            AssistantSettings.timezone,
            AssistantSettings.work_start,
            AssistantSettings.work_end,
            AssistantSettings.workdays,
            AssistantSettings.shift_override,
            AssistantSettings.shift_override_until,
            AssistantSettings.cold_start_started_at,
            AssistantSettings.cold_start_step,
            AssistantSettings.cold_start_outcome,
            AssistantSettings.cold_start_done_at,
        )
    )
    # 最近一次非所有者访问个人内容的时间（管理命令等）：只取时间，动作与对象不出现在这里。
    foreign = await access_log.last_foreign(session)
    now = utcnow()
    shifts: dict[uuid.UUID, tuple[bool, dict[str, Any]]] = {}
    for found in prefs.all():
        # 查出来的这一行带着 settings 的时间判断要读的全部明文列：直接当设置行用，不另建模型
        # 对象（没取加密列，也不该在这里出现一个能落库的设置行）。
        probe = cast(AssistantSettings, found)
        started, done = found.cold_start_started_at, found.cold_start_done_at
        shifts[found.bot_id] = (
            assistant_settings.on_shift(probe, now),
            {
                "state": assistant_settings.cold_start_state(probe),
                "step": found.cold_start_step,
                "outcome": found.cold_start_outcome,
                "started_at": started.isoformat() if started else None,
                "done_at": done.isoformat() if done else None,
            },
        )
    running_jobs = await jobs.active_counts(session)
    items = []
    for bot_id, state, enabled, created, owner_id, login, name, relay_id, relay, node in rows:
        flow = live.get(bot_id)
        working, cold = shifts.get(bot_id, (False, None))
        items.append(
            {
                "bot_id": str(bot_id),
                "state": state,
                "enabled": enabled,
                "created_at": created.isoformat() if created else None,
                "owner": {"id": str(owner_id), "login_name": login, "display_name": name},
                "relay": {"id": str(relay_id), "name": relay} if relay_id else None,
                "node": (
                    {"id": str(node.id), "name": node.name, "online": online(node)}
                    if node is not None
                    else None
                ),
                "lifecycle": (
                    {
                        "action": flow.action,
                        "step": flow.step,
                        "trigger": flow.trigger,
                        "error_code": flow.error_code,
                    }
                    if flow is not None
                    else None
                ),
                "on_shift": bool(enabled and state == "active" and working),
                "cold_start": cold,
                "directories_pending_removal": int(pending_dirs.get(bot_id, 0)),
                "jobs_active": running_jobs.get(bot_id, 0),
                "watch": health.get(bot_id),
                "turns": (
                    {"today": spent[bot_id].used, "budget": spent[bot_id].budget}
                    if bot_id in spent
                    else None
                ),
                "inbox_pending": waiting.get(bot_id, 0),
                "last_non_owner_access_at": (
                    foreign[bot_id].isoformat() if bot_id in foreign else None
                ),
            }
        )
    return {"code": 0, "data": {"items": items}}


@router.post("/{bot_id}/deprovision")
async def deprovision(
    bot_id: uuid.UUID,
    body: DeprovisionIn,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.confirm is not True:
        raise ApiError(422, 422, "停用不可撤销，请带上 confirm: true")
    bot = await session.get(Bot, bot_id)
    if bot is None or bot.kind != "assistant":
        raise not_found("个人助理不存在")
    state = await assistant_deprovision.begin(session, request, bot, trigger="admin", actor=user)
    await session.commit()
    return {"code": 0, "data": {"bot_id": str(bot_id), "state": state}}
