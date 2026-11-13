"""个人助理的设置：开通时记下的所有者身份，以及按所有者时区做的时间判断。

读写的是 `assistant_settings`（一位助理一行）。身份四列只由开通流程写（`record_identity`），
平台来源的轮次凭它们拿个人工具；工作时段、时区、重点群等由所有者在「我的助理」页与冷启动里改。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AssistantSettings
from coreman.core.timeutils import aware_utc, utcnow

DEFAULT_TIMEZONE = "Asia/Shanghai"


async def get(session: AsyncSession, bot_id: uuid.UUID) -> AssistantSettings:
    """这位助理的设置；没有就按默认值建一行。调用方提交。

    建行用 `ON CONFLICT DO NOTHING`：两个进程同时第一次读，得到的是同一行，不会有一个报错。
    """
    row = await session.get(AssistantSettings, bot_id)
    if row is None:
        await session.execute(
            insert(AssistantSettings)
            .values(bot_id=bot_id)
            .on_conflict_do_nothing(index_elements=[AssistantSettings.bot_id])
        )
        row = await session.get(AssistantSettings, bot_id, populate_existing=True)
    assert row is not None
    return row


async def record_identity(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    open_id: str,
    tenant_key: str,
    platform_user_id: str,
    chat_id: str,
) -> AssistantSettings:
    """记下所有者在飞书里的身份与他和助理的私聊。只由开通流程调用，值来自飞书给的操作人。"""
    row = await get(session, bot_id)
    row.open_id, row.tenant_key = open_id, tenant_key
    row.platform_user_id, row.chat_id = platform_user_id, chat_id
    row.updated_at = utcnow()
    return row


def identified(row: AssistantSettings) -> bool:
    """身份四列都记下了：缺任何一项，平台来源的轮次都不可用。"""
    return bool(row.open_id and row.tenant_key and row.platform_user_id and row.chat_id)


def zone(row: AssistantSettings) -> ZoneInfo:
    """所有者的时区。库里校验不了时区名：认不出的退回默认，轮询循环不会因此抛错。"""
    try:
        return ZoneInfo(row.timezone)
    except (KeyError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_today(settings: AssistantSettings, now: datetime) -> date:
    """所有者时区里的今天（会话每天换新、每日预算、当天到期的待跟进都按它）。"""
    return aware_utc(now).astimezone(zone(settings)).date()


def on_shift(settings: AssistantSettings, now: datetime) -> bool:
    """现在是不是所有者的工作时段。「上班」「下班」的临时切换没过期时以它为准。"""
    now = aware_utc(now)
    until = settings.shift_override_until
    if settings.shift_override is not None and (until is None or until > now):
        return settings.shift_override == "on"
    local = now.astimezone(zone(settings))
    clock = local.strftime("%H:%M")
    return local.isoweekday() in settings.workdays and (
        settings.work_start <= clock < settings.work_end
    )
