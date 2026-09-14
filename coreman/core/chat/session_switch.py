"""`sessions` / `会话列表` 多会话切换（spec §8.2 步骤 4；使用平台默认文案）。

历史会话不另建表：`chat_logs` 里每条消息都记了当时用的 `relay_session_id`，按它分组就是
「这个会话键跟这个机器人聊过哪几轮」。预览取该会话最早的一条文本消息——最早那句通常
就是这轮的主题，比取最后一句更认得出来。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.commands import normalize
from coreman.core.db.models import ChatLog
from coreman.core.i18n.messages import msg

SESSION_WORDS = ("sessions", "会话列表")
MAX_RECENT = 10
PREVIEW_LEN = 30
PENDING_SECONDS = 300


@dataclass(frozen=True)
class SessionPreview:
    """列表里的一行：切到哪条 relay 会话、显示什么、最后活跃于何时。"""

    relay_session_id: uuid.UUID
    preview: str
    last_at: datetime


def is_sessions_command(text: str) -> bool:
    """精确匹配才算命令：「会话」「sessions list」都是正经话，不该被当成列表指令吞掉。"""
    return normalize(text).lower() in SESSION_WORDS


def parse_choice(text: str, n: int) -> int | None:
    """纯数字且落在 1..n 才算选序号，其余（「二」「1a」「0」）都是普通消息。

    守卫用 `isdecimal()` 而不是 `isdigit()`：后者对「②」「²」也为真，而 `int()` 只吃 Nd 类，
    这一步放过去下一行就抛 ValueError——任务崩在开流之前，用户连一句错误提示都收不到。
    全角「１」两者都认，当成 1 是合理的。
    """
    value = normalize(text)
    if not value.isdecimal():
        return None
    index = int(value)
    return index if 1 <= index <= n else None


def relative_time(dt: datetime, now: datetime, locale: str = "zh") -> str:
    """相对时间。时钟漂移让 dt 落在 now 之后时按「刚刚」算，不显示负数。"""
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return msg("rt_just_now", locale)
    if seconds < 3600:
        return msg("rt_minutes", locale, n=seconds // 60)
    if seconds < 86400:
        return msg("rt_hours", locale, n=seconds // 3600)
    if seconds < 2 * 86400:
        return msg("rt_yesterday", locale)
    return msg("rt_days", locale, n=seconds // 86400)


def format_list(items: list[SessionPreview], now: datetime, locale: str = "zh") -> str:
    lines = [msg("sessions_header", locale, n=len(items)), ""]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item.preview}（{relative_time(item.last_at, now, locale)}）")
    return "\n".join(lines)


async def recent_sessions(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    relay_server_id: uuid.UUID | None,
    limit: int = MAX_RECENT,
    locale: str = "zh",
) -> list[SessionPreview]:
    """最近 N 个 relay 会话（当前 relay 或未记录 relay 的），每个取最早一条文本消息的前 30 字。

    只列当前 relay 上的会话：relay 会话 id 是那台实例自己的，切到别的实例上的旧会话等于
    切到一个不存在的 id。`relay_server_id IS NULL` 的老行一并放行——M2 之前没记这一列，
    把它们全挡掉等于说「你从没聊过天」。
    """
    last_at = func.max(ChatLog.request_at).label("last_at")
    conds = [
        ChatLog.bot_id == bot_id,
        ChatLog.session_key == session_key,
        ChatLog.relay_session_id.is_not(None),
    ]
    if relay_server_id is not None:
        conds.append(
            or_(ChatLog.relay_server_id == relay_server_id, ChatLog.relay_server_id.is_(None))
        )
    rows = (
        await session.execute(
            select(ChatLog.relay_session_id, last_at)
            .where(*conds)
            .group_by(ChatLog.relay_session_id)
            .order_by(last_at.desc())
            .limit(limit)
        )
    ).all()
    order: list[uuid.UUID] = []
    seen_at: dict[uuid.UUID, datetime] = {}
    for rs, at in rows:
        if rs is None:
            continue
        order.append(rs)
        seen_at[rs] = at
    if not order:
        return []
    # bot_id / session_key 一并带上：只按 relay_session_id 查走不了
    # `chat_logs_session_idx`（前导列缺失），在一张只增不减的表上就是全表扫。
    firsts = (
        await session.execute(
            select(ChatLog.relay_session_id, ChatLog.message_content)
            .where(
                ChatLog.bot_id == bot_id,
                ChatLog.session_key == session_key,
                ChatLog.relay_session_id.in_(order),
                ChatLog.message_type == "text",
                ChatLog.message_content.is_not(None),
            )
            .order_by(ChatLog.request_at.asc())
        )
    ).all()
    preview: dict[uuid.UUID, str] = {}
    for rs, content in firsts:
        if rs is None or content is None:
            continue
        preview.setdefault(rs, content[:PREVIEW_LEN])
    return [
        SessionPreview(rs, preview.get(rs) or msg("non_text_message", locale), seen_at[rs])
        for rs in order
    ]
