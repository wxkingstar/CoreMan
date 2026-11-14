"""个人助理的设置：开通时记下的所有者身份，以及按所有者时区做的时间判断。

读写的是 `assistant_settings`（一位助理一行）。身份四列只由开通流程写（`record_identity`），
平台来源的轮次凭它们拿个人工具；全天候还是按工作时段、工作时段、时区、重点群等由所有者在
「我的助理」页与冷启动里改。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
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


def owed(settings: AssistantSettings, day: date) -> bool:
    """`day` 那天还没处理过下班编译。"""
    return settings.compiled_on is None or settings.compiled_on < day


def mark_worked(settings: AssistantSettings, now: datetime) -> bool:
    """记下今天上过班（下班编译凭它触发），返回记了没有。

    `worked_on` 是今天之前、且那天还欠着整理时不覆盖：留给下班编译先为那一天整理（全天候、过了
    零点运行时暂停时就是这样）。`worked_on` 晚于今天（所有者把时区往西改）时直接记成今天。
    """
    today = local_today(settings, now)
    day = settings.worked_on
    if day is not None and day < today and owed(settings, day):
        return False
    settings.worked_on = today
    return True


def on_shift(settings: AssistantSettings, now: datetime) -> bool:
    """现在在不在班：「上班」「下班」的临时切换没过期时以它为准；否则全天候就在班，不是全天候
    按工作时段。"""
    now = aware_utc(now)
    until = settings.shift_override_until
    if settings.shift_override is not None and (until is None or until > now):
        return settings.shift_override == "on"
    if settings.always_on:
        return True
    local = now.astimezone(zone(settings))
    clock = local.strftime("%H:%M")
    return local.isoweekday() in settings.workdays and (
        settings.work_start <= clock < settings.work_end
    )


# 「下班」找下一个上班时刻时往后看几天：一周七天加当天。
_LOOKAHEAD_DAYS = 8
# 没有工作日可找时「下班」持续多久。
_NO_WORKDAY_REST = timedelta(days=1)


def _at(row: AssistantSettings, day: date, clock: str) -> datetime:
    """所有者时区里某天某个 `HH:MM` 对应的时刻（UTC）。"""
    hour, minute = clock.split(":")
    return datetime.combine(day, time(int(hour), int(minute)), tzinfo=zone(row)).astimezone(UTC)


def end_today(row: AssistantSettings, now: datetime) -> datetime | None:
    """今天是工作日且还没到下班时间：今天的下班时刻；否则 None。"""
    day = local_today(row, now)
    if day.isoweekday() not in row.workdays:
        return None
    end = _at(row, day, row.work_end)
    return end if end > aware_utc(now) else None


def next_start(row: AssistantSettings, now: datetime) -> datetime | None:
    """晚于 `now` 的下一个上班时刻；没有工作日时为 None。"""
    today = local_today(row, now)
    for offset in range(_LOOKAHEAD_DAYS):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in row.workdays:
            continue
        start = _at(row, day, row.work_start)
        if start > aware_utc(now):
            return start
    return None


def next_midnight(row: AssistantSettings, now: datetime) -> datetime:
    """所有者时区的次日零点。"""
    return _at(row, local_today(row, now) + timedelta(days=1), "00:00")


def override(row: AssistantSettings, now: datetime) -> str | None:
    """还没过期的临时切换（`on` / `off`）；没有或已过期返回 None。"""
    until = row.shift_override_until
    if row.shift_override is None or (until is not None and until <= aware_utc(now)):
        return None
    return row.shift_override


def clear_override(row: AssistantSettings) -> None:
    row.shift_override, row.shift_override_until = None, None
    row.updated_at = utcnow()


def clock_in(row: AssistantSettings, now: datetime) -> datetime | None:
    """所有者说「上班」：到今天的下班时间为止；今天不是工作日或已过下班时间，到次日零点。

    返回到期时刻。到点不用清理：`on_shift` 只认没过期的临时切换。全天候时本来就在班：只清掉
    「下班」的临时切换，返回 None。
    """
    if row.always_on:
        clear_override(row)
        return None
    until = end_today(row, now) or next_midnight(row, now)
    row.shift_override, row.shift_override_until = "on", until
    row.updated_at = utcnow()
    return until


def clock_out(row: AssistantSettings, now: datetime) -> datetime:
    """所有者说「下班」：到下一个工作时段开始为止。返回到期时刻。

    全天候时同样按存着的工作时段找下一个上班时刻：到点之后恢复全天候。
    """
    until = next_start(row, now) or aware_utc(now) + _NO_WORKDAY_REST
    row.shift_override, row.shift_override_until = "off", until
    row.updated_at = utcnow()
    return until


# 重点群每轮直接读历史，计入分钟预算：与 `watch.FOCUS_MAX` 一致。
MAX_FOCUS = 5
MAX_IGNORED = 50
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_CHAT = re.compile(r"^oc_[A-Za-z0-9_-]{1,80}$")


class SettingsError(Exception):
    """这次修改被拒绝；`code` 给调用方（助理工具、「我的助理」页），`detail` 是改正时用得上的数。"""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, **self.detail}


@lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _timezone(name: str) -> str:
    name = name.strip()
    if name not in _zones():
        raise SettingsError("invalid_timezone")
    return name


def _clock(value: str) -> str:
    if not _HHMM.match(value):
        raise SettingsError("invalid_work_hours")
    return value


def _days(values: list[int]) -> list[int]:
    if (
        not values
        or len(set(values)) != len(values)
        or any(type(day) is not int or not 1 <= day <= 7 for day in values)
    ):
        raise SettingsError("invalid_workdays")
    return sorted(values)


def _chats(values: list[str], limit: int) -> list[str]:
    found = list(dict.fromkeys(value.strip() for value in values))
    if any(not _CHAT.match(value) for value in found):
        raise SettingsError("invalid_chats")
    if len(found) > limit:
        raise SettingsError("too_many_chats", limit=limit)
    return found


def cold_start_state(row: AssistantSettings) -> str:
    """冷启动走到哪了：`pending`（没开始）、`in_progress`、`done`（做完、跳过或超时）。"""
    if row.cold_start_done_at is not None:
        return "done"
    return "in_progress" if row.cold_start_started_at is not None else "pending"


def view(row: AssistantSettings, now: datetime) -> dict[str, Any]:
    """给所有者看的设置（助理工具与「我的助理」页共用）。`row` 的内容列要已还原。"""
    switched = override(row, now)
    until = row.shift_override_until if switched is not None else None
    return {
        "timezone": zone(row).key,
        "always_on": row.always_on,
        "work_start": row.work_start,
        "work_end": row.work_end,
        "workdays": sorted(row.workdays),
        "focus_chats": list(row.focus_chats_enc or []),
        "ignored_chats": list(row.ignored_chats_enc or []),
        "on_shift": on_shift(row, now),
        "shift_override": switched,
        "shift_override_until": until.isoformat() if until is not None else None,
        "cold_start": cold_start_state(row),
        "today": local_today(row, now).isoformat(),
    }


async def update(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    now: datetime,
    timezone: str | None = None,
    always_on: bool | None = None,
    work_start: str | None = None,
    work_end: str | None = None,
    workdays: list[int] | None = None,
    focus_chats: list[str] | None = None,
    ignored_chats: list[str] | None = None,
) -> AssistantSettings:
    """改这位助理的设置：只改给出的项，名单整份替换。调用方提交。

    改了全天候、时区、工作时段或工作日时清掉「上班」「下班」的临时切换：它是按旧的时段算出来的。
    从忽略名单里移出的会话，盯消息的游标置为 `now`（会等正在进行的一轮轮询提交）。

    Raises:
        SettingsError: `nothing_to_update`、`invalid_timezone`、`invalid_work_hours`（格式不对，
            或上班时间不早于下班时间）、`invalid_workdays`、`invalid_chats`、`too_many_chats`、
            `chat_in_both_lists`、`content_locked`（取不到数据密钥时不能写名单）
    """
    given = (timezone, always_on, work_start, work_end, workdays, focus_chats, ignored_chats)
    if all(value is None for value in given):
        raise SettingsError("nothing_to_update")
    await get(session, bot_id)
    # 先把调用方在这一行上没落库的改动刷下去，再加锁重读。
    await session.flush()
    row = await session.get(AssistantSettings, bot_id, with_for_update=True, populate_existing=True)
    assert row is not None
    zone_name = _timezone(timezone) if timezone is not None else row.timezone
    start = _clock(work_start) if work_start is not None else row.work_start
    end = _clock(work_end) if work_end is not None else row.work_end
    if start >= end:
        raise SettingsError("invalid_work_hours")
    days = _days(workdays) if workdays is not None else sorted(row.workdays)
    focus = _chats(focus_chats, MAX_FOCUS) if focus_chats is not None else None
    ignored = _chats(ignored_chats, MAX_IGNORED) if ignored_chats is not None else None
    lists = focus is not None or ignored is not None
    if lists:
        now_focus = focus if focus is not None else list(row.focus_chats_enc or [])
        now_ignored = ignored if ignored is not None else list(row.ignored_chats_enc or [])
        if set(now_focus) & set(now_ignored):
            raise SettingsError("chat_in_both_lists")
        # 名单是加密列：密钥不可用时写下去会丢内容，宁可拒绝。
        if not await sealed.key_ready(session, bot_id):
            raise SettingsError("content_locked")
    every = always_on if always_on is not None else row.always_on
    schedule = (every, zone_name, start, end, days)
    current = (row.always_on, row.timezone, row.work_start, row.work_end, sorted(row.workdays))
    if schedule != current:
        row.always_on, row.timezone, row.work_start, row.work_end, row.workdays = schedule
        clear_override(row)
    if focus is not None:
        row.focus_chats_enc = focus
    if ignored is not None:
        released = [chat for chat in row.ignored_chats_enc or [] if chat not in ignored]
        row.ignored_chats_enc = ignored
        if released:
            # 移出忽略名单的会话从这一刻起盯：忽略期间的消息不补进收件箱。
            # 函数内引入：watch 在模块层引用本模块。
            from coreman.core.assistants import watch

            await watch.restart_chats(session, bot_id, released, now)
    row.updated_at = utcnow()
    await session.flush()
    return row
