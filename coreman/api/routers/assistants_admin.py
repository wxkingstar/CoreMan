"""平台管理员看个人助理：谁开通了、在哪个运行时、开通与停用走到哪一步；可以停用某人的助理。

只出元数据（人、实例、节点、状态、时间、错误代码、盯消息的健康状态、今天的轮次与预算、待交回的
条目数），不碰任何内容列，也不解密任何东西。停用只回「已开始停用」，不回记录本身。开通范围与
可选 AI 在平台设置里改（`assistant_eligibility`、`assistant_backends`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.assistants import delivery, deprovisioning, lifecycle
from coreman.core.audit import record_audit
from coreman.core.db.models import (
    AssistantInbox,
    AssistantLifecycle,
    AssistantRetiredDir,
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
    items = []
    for bot_id, state, enabled, created, owner_id, login, name, relay_id, relay, node in rows:
        flow = live.get(bot_id)
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
                "directories_pending_removal": int(pending_dirs.get(bot_id, 0)),
                "watch": health.get(bot_id),
                "turns": (
                    {"today": spent[bot_id].used, "budget": spent[bot_id].budget}
                    if bot_id in spent
                    else None
                ),
                "inbox_pending": waiting.get(bot_id, 0),
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
    prior = await lifecycle.live(session, bot.owner_id, lock=False) if bot.owner_id else None
    before = (prior.id, prior.action, prior.step) if prior is not None else None
    flow = await deprovisioning.request(session, bot, trigger="admin", actor_id=user.id)
    # 只在这一次真的开始了停用时记审计（新开的停用流程，或把宽限期里的停用推进到现在）；
    # 重复点、已经停用完的都不记。
    started = flow is not None and (
        before is None
        or before[0] != flow.id
        or before[1] != "deprovision"
        or (before[2] == "grace" and flow.step != "grace")
    )
    if started:
        await record_audit(
            session,
            action="assistant.deprovision",
            actor_id=user.id,
            actor_login=user.login_name,
            target_type="bot",
            target_id=str(bot_id),
            diff={},
            ip=client_ip(request),
        )
    await session.commit()
    return {
        "code": 0,
        "data": {"bot_id": str(bot_id), "state": "deprovisioning" if flow else "deprovisioned"},
    }
