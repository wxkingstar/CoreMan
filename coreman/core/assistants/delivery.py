"""投递：把盯消息攒在收件箱里的条目交回助理的主会话，并管着每日轮次预算。

scheduler 的 `_assistant_delivery_loop` 每 5 秒调一次 `tick`：先对账（已经结束的交回轮次把它
带的条目记账），再给有到点条目的助理各排一轮 `assistant_handover`。同一位助理同一时刻至多
一轮；轮次正常结束，条目才记为已交回，否则回到待交回、下一轮再带上。

这里只读写元数据：条目的编号、级别、时间与绑定的任务号。消息内容在 `summary_enc`（密文）里，
只有 `due_items` 与 `load` 会还原，后者供 worker 开轮时渲染整理稿。日志只带编号与计数。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility
from coreman.core.assistants import settings as assistant_settings
from coreman.core.db.models import AssistantSettings, Bot, ChatLog
from coreman.core.logging import get_logger

log = get_logger(__name__)

BUDGET_KEY = "assistant_daily_turn_budget"
DEFAULT_BUDGET = 200
BUDGET_MIN, BUDGET_MAX = 10, 2000
# 预算用完后，立即交回级别的条目至多再透支这么多轮。
OVERDRAFT = 20


def zone_named(name: str | None) -> ZoneInfo:
    """按名字取时区；库里校验不了时区名，认不出的退回默认。"""
    try:
        return ZoneInfo(name or assistant_settings.DEFAULT_TIMEZONE)
    except (KeyError, ValueError, OSError):
        return ZoneInfo(assistant_settings.DEFAULT_TIMEZONE)


def day_bounds(zone: ZoneInfo, today: date) -> tuple[datetime, datetime]:
    """这个时区里 `today` 这一天的起止（UTC，左闭右开）；夏令时切换日不是 24 小时。"""
    start = datetime.combine(today, time.min, tzinfo=zone)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


async def default_budget(session: AsyncSession) -> int:
    """平台默认的每日轮次预算；设置被写坏（管理接口有校验，坏值只可能来自直接改库）时用 200。"""
    raw = await eligibility.setting(session, BUDGET_KEY)
    if isinstance(raw, int) and not isinstance(raw, bool) and BUDGET_MIN <= raw <= BUDGET_MAX:
        return raw
    return DEFAULT_BUDGET


async def turns_used(session: AsyncSession, bot_id: uuid.UUID, zone: ZoneInfo, today: date) -> int:
    """这位助理今天跑了几轮：数对话记录。

    所有者私聊的轮次、审批结果回合、交回轮次都在里面；记录在开流时写入，在跑的也算。
    """
    start, end = day_bounds(zone, today)
    found = await session.scalar(
        select(func.count(ChatLog.id)).where(
            ChatLog.bot_id == bot_id, ChatLog.request_at >= start, ChatLog.request_at < end
        )
    )
    return int(found or 0)


async def budget_left(session: AsyncSession, bot: Bot, today: date) -> int:
    """今天还剩几轮预算；可以为负，负多少就是已经透支了多少轮。

    Args:
        today: 所有者时区里的今天（`settings.local_today`）
    """
    prefs = await session.get(AssistantSettings, bot.id)
    own = prefs.daily_turn_budget if prefs is not None else None
    total = own if own is not None else await default_budget(session)
    zone = zone_named(prefs.timezone if prefs is not None else None)
    return total - await turns_used(session, bot.id, zone, today)
