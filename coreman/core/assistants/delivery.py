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
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility, platform_turns, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox, tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.db.models import (
    AssistantDelivery,
    AssistantInbox,
    AssistantSettings,
    Bot,
    ChatLog,
    Task,
)
from coreman.core.logging import get_logger

log = get_logger(__name__)

BUDGET_KEY = "assistant_daily_turn_budget"
DEFAULT_BUDGET = 200
BUDGET_MIN, BUDGET_MAX = 10, 2000
# 预算用完后，立即交回级别的条目至多再透支这么多轮。
OVERDRAFT = 20
# 一轮最多带这么多条目；多出来的留给下一轮。
MAX_ITEMS = 30
# 带着某个条目的交回轮次真失败满这么多次，这个条目作废。
MAX_FAILURES = 3
# 没跑成的轮次把条目放回之后，至少过这么久才再排（所有者正在聊、有公告时不空转）。
RETRY_AFTER_RELEASE = timedelta(seconds=60)
RETRY_AFTER_FAILURE = timedelta(minutes=5)
# 任务记了失败、但只是「没轮到」：不算条目的失败次数。
QUIET_CODES = frozenset(
    {"assistant_paused", "platform_turn_yielded", "relay_unavailable", "session_busy"}
)
RECONCILE_BATCH = 200
BUDGET_NOTICE = (
    "今天的轮次预算（{total} 轮）已经用完：不紧急的事项留到明天再交回，紧急的照常处理。"
    "需要调整额度请联系管理员。"
)


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


def gate(bot: Bot, prefs: AssistantSettings | None, now: datetime) -> bool:
    """这位助理现在该不该有平台发起的动作（交回、顺带检查）：启用中、开通好、冷启动做完、在上班。

    运行时暂停不在这里判断：暂停只是不入队，调用方另看 `runtime.turn_blocker`。
    """
    return bool(
        bot.enabled
        and is_assistant(bot)
        and bot.assistant_state == "active"
        and bot.owner_id is not None
        and prefs is not None
        and assistant_settings.identified(prefs)
        and prefs.cold_start_done_at is not None
        and assistant_settings.on_shift(prefs, now)
    )


async def state(
    session: AsyncSession, bot_id: uuid.UUID, *, lock: bool = False
) -> AssistantDelivery | None:
    """这位助理的投递状态行，没有就建。

    `lock=True` 时取行锁并持有到事务结束；别处正锁着（另一个进程在为他入队，或顺带检查正在
    调飞书）就返回 None，调用方这一次什么都不做。
    """
    await session.execute(
        insert(AssistantDelivery)
        .values(bot_id=bot_id)
        .on_conflict_do_nothing(index_elements=[AssistantDelivery.bot_id])
    )
    stmt = (
        select(AssistantDelivery)
        .where(AssistantDelivery.bot_id == bot_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    found: AssistantDelivery | None = await session.scalar(stmt)
    return found


def _waiting(bot_id: uuid.UUID, now: datetime) -> tuple[Any, ...]:
    """到点、还没装进任何一轮的条目。"""
    return (
        AssistantInbox.bot_id == bot_id,
        AssistantInbox.status == "pending",
        AssistantInbox.delivered_task_id.is_(None),
        or_(AssistantInbox.hold_until.is_(None), AssistantInbox.hold_until <= now),
    )


def _count(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def due_items(
    session: AsyncSession, bot_id: uuid.UUID, now: datetime
) -> list[AssistantInbox]:
    """到了该交回的条目：立即交回的、攒批到点的、压着到点的。内容已还原。"""
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(*_waiting(bot_id, now))
            .order_by(AssistantInbox.id)
            .limit(MAX_ITEMS)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows


async def _in_flight(session: AsyncSession, bot_id: uuid.UUID) -> bool:
    """这位助理有没有还没了结的交回：绑着任务号没记账的条目，或没结束的交回任务。"""
    held = await session.scalar(
        select(AssistantInbox.id)
        .where(
            AssistantInbox.bot_id == bot_id,
            AssistantInbox.status == "pending",
            AssistantInbox.delivered_task_id.is_not(None),
        )
        .limit(1)
    )
    if held is not None:
        return True
    queued = await session.scalar(
        select(Task.id)
        .where(Task.bot_id == bot_id, Task.kind == HANDOVER_KIND, Task.status.in_(tasks.OPEN))
        .limit(1)
    )
    return queued is not None


async def _pick(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, now: datetime
) -> tuple[list[int], bool]:
    """这一轮带哪些条目：(条目编号, 有没有条目因为预算留在了箱里)。只读元数据。"""
    today = assistant_settings.local_today(prefs, now)
    left = await budget_left(session, bot, today)
    ids = select(AssistantInbox.id).where(*_waiting(bot.id, now)).order_by(AssistantInbox.id)
    if left > 0:
        return list(await session.scalars(ids.limit(MAX_ITEMS))), False
    # 预算用完：只有立即交回的还走，且至多再透支 OVERDRAFT 轮。
    picked: list[int] = []
    if left > -OVERDRAFT:
        urgent = ids.where(AssistantInbox.level == "immediate").limit(MAX_ITEMS)
        picked = list(await session.scalars(urgent))
    waiting = await session.scalar(
        select(func.count(AssistantInbox.id)).where(*_waiting(bot.id, now))
    )
    return picked, int(waiting or 0) > len(picked)


async def _tell_budget_spent(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, row: AssistantDelivery, today: date
) -> None:
    """预算用完后第一次有条目没交回：私信所有者一次（每天至多一次），不带任何内容。"""
    if row.budget_notified_on == today:
        return
    own = prefs.daily_turn_budget
    total = own if own is not None else await default_budget(session)
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-budget:{bot.id}:{today.isoformat()}",
        target={"chat_id": prefs.chat_id},
        payload={"markdown": BUDGET_NOTICE.format(total=total)},
    )
    row.budget_notified_on = today
    log.info("assistant_budget_spent", bot_id=str(bot.id))


async def dispatch(session: AsyncSession, bot: Bot, now: datetime) -> Task | None:
    """有到点的条目、且这位助理没有还没了结的交回时，排一轮 `assistant_handover`。调用方提交。

    装进这一轮的条目写上 `delivered_task_id`（状态仍是 `pending`）：从这一刻起盯消息不再改
    它们，直到这一轮结束时由 `settle` / `reconcile` 记账。运行时暂停、有公告、下班、冷启动没
    做完时不入队，条目留在箱里，这里不为此发任何私信。
    """
    prefs = await session.get(AssistantSettings, bot.id)
    if prefs is None or not gate(bot, prefs, now):
        return None
    row = await state(session, bot.id, lock=True)
    if row is None or await _in_flight(session, bot.id):
        return None
    if await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id):
        return None
    if await runtime.turn_blocker(session, bot) is not None:
        return None
    ids, deferred = await _pick(session, bot, prefs, now)
    if deferred:
        await _tell_budget_spent(
            session, bot, prefs, row, assistant_settings.local_today(prefs, now)
        )
    if not ids:
        return None
    row.handover_seq += 1
    row.updated_at = now
    task = await platform_turns.enqueue(
        session,
        bot,
        HANDOVER_KIND,
        {"items": ids},
        dedupe_key=f"assistant-handover:{bot.id}:{row.handover_seq}",
    )
    if task is None:
        return None
    await session.execute(
        update(AssistantInbox)
        .where(
            AssistantInbox.id.in_(ids),
            AssistantInbox.bot_id == bot.id,
            AssistantInbox.status == "pending",
            AssistantInbox.delivered_task_id.is_(None),
        )
        .values(delivered_task_id=task.id, updated_at=func.now())
    )
    log.info("assistant_handover_queued", bot_id=str(bot.id), task_id=task.id, items=len(ids))
    return task


async def bound(session: AsyncSession, task: Task) -> list[AssistantInbox]:
    """绑在这一轮上、还没记账的条目，内容已还原；读不出来的（密钥已销毁、密文损坏）跳过。

    纯读：worker 每次认领这一轮都会调它（让路放回队列后再认领也是），不能改任何状态。
    """
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(
                AssistantInbox.bot_id == task.bot_id,
                AssistantInbox.delivered_task_id == task.id,
                AssistantInbox.status == "pending",
            )
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return [row for row in rows if not sealed.is_locked(row) and not sealed.is_undecryptable(row)]


async def _settle(session: AsyncSession, task_id: int, ok: bool) -> int:
    mine = (AssistantInbox.delivered_task_id == task_id, AssistantInbox.status == "pending")
    if ok:
        done = await session.execute(
            update(AssistantInbox).where(*mine).values(status="delivered", updated_at=func.now())
        )
        return _count(done)
    ended = (
        await session.execute(select(Task.status, Task.error_code).where(Task.id == task_id))
    ).first()
    # 真失败：任务记了失败，原因又不是「没轮到」。取消、任务行已不在的都不算。
    failed = ended is not None and ended[0] == "failed" and ended[1] not in QUIET_CODES
    retry = func.now() + (RETRY_AFTER_FAILURE if failed else RETRY_AFTER_RELEASE)
    values: dict[str, Any] = {
        "delivered_task_id": None,
        "hold_until": func.greatest(func.coalesce(AssistantInbox.hold_until, retry), retry),
        "updated_at": func.now(),
    }
    if failed:
        values["failures"] = AssistantInbox.failures + 1
    released = await session.execute(
        update(AssistantInbox).where(*mine).values(**values).returning(AssistantInbox.id)
    )
    ids = [int(found[0]) for found in released]
    if failed and ids:
        await session.execute(
            update(AssistantInbox)
            .where(AssistantInbox.id.in_(ids), AssistantInbox.failures >= MAX_FAILURES)
            .values(status="void", void_code="undeliverable", updated_at=func.now())
        )
    return len(ids)


async def settle(session: AsyncSession, task: Task, ok: bool) -> int:
    """一轮交回结束后记账，返回记了几条。调用方提交；重复调用无事发生。

    Args:
        ok: 这一轮正常结束（任务 `succeeded`）。为真时条目记为已交回；否则条目回到待交回
            （裁定 3）——被所有者的消息取消（`superseded`）、让路放弃（`platform_turn_yielded`）、
            暂停（`assistant_paused`）、失败都一样，下一轮再带上；真失败另计次数。
    """
    return await _settle(session, task.id, ok)


async def reconcile(session: AsyncSession) -> int:
    """绑着的任务已经结束、却还没记账的条目统一记账，返回条数。调用方提交。

    没跑到收尾的轮次都靠这里：开流前被取消、暂停、让路放弃、公告拦下、worker 失联被收尸；
    任务行已经被清理掉的按没成功算。
    """
    found = (
        await session.execute(
            select(AssistantInbox.delivered_task_id, Task.status)
            .select_from(AssistantInbox)
            .outerjoin(Task, Task.id == AssistantInbox.delivered_task_id)
            .where(
                AssistantInbox.status == "pending",
                AssistantInbox.delivered_task_id.is_not(None),
                or_(Task.id.is_(None), Task.status.not_in(tasks.OPEN)),
            )
            .distinct()
            .limit(RECONCILE_BATCH)
        )
    ).all()
    total = 0
    for task_id, status in found:
        total += await _settle(session, int(task_id), status == "succeeded")
    return total
