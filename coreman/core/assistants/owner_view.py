"""「我的助理」页的读取：本人的助理是哪一条、概览、没有助理时的开通指引。

只给所有者本人用：调用方（`/api/me/assistant` 的路由）传进来的 `bot` 一律由登录人推出。概览只有
元数据——状态、运行时的名字、是否在班、轮次、轮询健康——不读任何内容列。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, eligibility, lifecycle, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.config import get_settings
from coreman.core.db.models import (
    AssistantLifecycle,
    AssistantWatch,
    Bot,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import online

# 开通的三步，页面按这些代号显示文案：私聊助理应用 → 点卡片上的「开通」→ 完成飞书授权。
HOW_TO = ("open_private_chat", "accept_offer", "authorize")


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
    url = (get_settings().tower_web_url or "").strip()
    return url if url.startswith(("https://", "http://")) else None


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
        "cold_start": {
            "state": assistant_settings.cold_start_state(prefs),
            "step": prefs.cold_start_step,
            "outcome": prefs.cold_start_outcome,
        },
        "turns": {
            "today": used,
            "budget": own if own is not None else await delivery.default_budget(session),
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
