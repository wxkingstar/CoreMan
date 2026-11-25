"""业务系统发起的任务碰到运行时额度上限：不换运行时（会把整个 AI 员工换走），排队等额度恢复。

开轮前已知额度用完：直接把这一轮放回队列，到重置时间再认领，员工那边什么都不发。
跑到一半才撞上：这一轮照常结束，在同一个独立会话里另排一轮，到点用原来的内容重跑，员工
不用重新提交。从第一次等额度起超过 7 天还没跑成就放弃。见
docs/superpowers/specs/2026-11-24-api-tasks-system-clients.md 第 6.5 节。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import ApiTask, RelayServer, Task
from coreman.core.timeutils import aware_utc

WAIT_MAX = timedelta(days=7)
# 拿不到重置时间时隔多久再试一次。
RETRY = timedelta(minutes=30)
# 用量到这个百分比算用完。
FULL = 100
EXPIRED_CODE = "quota_wait_expired"
EXPIRED_MESSAGE = "运行时额度等待超过 7 天仍未恢复，这条任务没有执行"


def exhausted_until(relay: RelayServer, now: datetime) -> datetime | None:
    """已知用完的额度窗口里最晚的重置时间；没用完或不知道返回 None。"""
    windows = (
        (relay.rate_limit_5h_used_pct, relay.rate_limit_5h_resets_at),
        (relay.rate_limit_7d_used_pct, relay.rate_limit_7d_resets_at),
    )
    found = [
        aware_utc(resets)
        for used, resets in windows
        if used is not None and float(used) >= FULL and resets is not None
        if aware_utc(resets) > now
    ]
    return max(found) if found else None


def resume_at(relay: RelayServer | None, now: datetime) -> datetime:
    known = exhausted_until(relay, now) if relay is not None else None
    return known or now + RETRY


def expired(row: ApiTask, now: datetime) -> bool:
    since = row.quota_waiting_since
    return since is not None and now - aware_utc(since) > WAIT_MAX


def mark_waiting(row: ApiTask, at: datetime, now: datetime) -> None:
    row.quota_waiting_since = row.quota_waiting_since or now
    row.resume_at = at


async def requeue(
    session: AsyncSession,
    task: Task,
    row: ApiTask,
    relay: RelayServer | None,
    now: datetime,
) -> datetime | None:
    """撞了额度的这一轮之后再排一轮；返回预计重跑时间，等太久放弃时返回 None。"""
    if expired(row, now):
        return None
    at = resume_at(relay, now)
    from coreman.core.systems_catalog.context import carried

    payload: dict[str, Any] = carried(task.payload)
    retry = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=task.bot_id,
            kind="chat",
            user_id=task.user_id,
            session_key=task.session_key,
            inbound_event_id=task.inbound_event_id,
            dedupe_key=f"api-task:{row.id}:quota:{task.id}",
            payload=payload,
            run_after=at,
        ),
    )
    if retry is None:
        return None
    row.task_id = retry.id
    mark_waiting(row, at, now)
    await session.flush()
    return at


def notice(at: datetime, now: datetime) -> str:
    """撞额度时私聊里那一轮末尾加的一句：不用重新提交，到点自动重跑（时间按看的人的时区显示）。"""
    from coreman.core.chat.liveness import clock

    return (
        f"\n\n---\n⏳ 运行时额度已用完，这条任务已排队，预计 {clock(at, now)} 恢复后自动重跑，"
        "不用重新提交。"
    )
