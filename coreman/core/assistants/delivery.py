"""投递：把盯消息攒在收件箱里的条目交回助理的主会话，并管着每日轮次预算。

scheduler 的 `_assistant_delivery_loop` 每 5 秒调一次 `tick`：先对账（已经结束的交回轮次把它
带的条目记账），再给有到点条目的助理各排一轮 `assistant_handover`。同一位助理同一时刻至多
一轮；轮次正常结束，条目才记为已交回，否则回到待交回、下一轮再带上。

这里只读写元数据：条目的编号、级别、时间与绑定的任务号。消息内容在 `summary_enc`（密文）里，
只有 `due_items` 与 `load` 会还原，后者供 worker 开轮时渲染整理稿。日志只带编号与计数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    User,
    UserIdentity,
)
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc

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
# 一轮交回没走完（被所有者的消息取消、让路放弃、暂停、失败），条目放回之后过多久才再排：
# 第一次这么久，连续没走完时逐次加倍，到上限为止；走完一轮从头算。所有者持续在聊时每一轮都会
# 被他的下一条消息取消——他本人在场，这些事本来就该等，不必每分钟开一轮又被打断。等待期间新
# 到的条目也不另开一轮。
RETRY_AFTER_RELEASE = timedelta(seconds=60)
RETRY_MAX = timedelta(minutes=15)
RETRY_AFTER_FAILURE = timedelta(minutes=5)
# 任务记了失败、但只是「没轮到」：不算条目的失败次数。
QUIET_CODES = frozenset(
    {"assistant_paused", "platform_turn_yielded", "relay_unavailable", "session_busy"}
)
RECONCILE_BATCH = 200
# 一次 tick 最多看多少位有到点条目的助理（下班、暂停的也在里面，逐位判断很便宜）。
TICK_BATCH = 200
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


def effective_budget(own: int | None, default: int) -> int:
    """实际生效的每日轮次预算：个人值与平台默认里较小的那个；没设个人值就是平台默认。

    个人值只能把预算调低。管理员之后把平台默认调到某人的个人值以下时，那个人也跟着收紧。
    """
    return default if own is None else min(own, default)


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
    total = effective_budget(own, await default_budget(session))
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


def retry_delay(misses: int) -> timedelta:
    """连续 `misses` 轮没走完之后，下一轮至少等多久：60 秒起逐次加倍，封顶 15 分钟。"""
    seconds = RETRY_AFTER_RELEASE.total_seconds() * 2 ** min(max(misses, 1) - 1, 10)
    return min(timedelta(seconds=seconds), RETRY_MAX)


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


async def _owner_ready(session: AsyncSession, bot: Bot) -> bool:
    """所有者现在接不接得住一轮：账号在用、飞书身份还绑着。

    与 worker 开轮时的核对同一口径（`PlatformTurnHandler._resolve`）：不成立时那一轮只会被取消
    （`platform_owner_changed`），条目放回、过一会儿又排，白白空转。入队前先看，不排。
    """
    owner = await session.get(User, bot.owner_id, populate_existing=True) if bot.owner_id else None
    if owner is None or owner.status != "active" or owner.source == "bootstrap":
        return False
    bound = await session.scalar(
        select(UserIdentity.user_id)
        .where(UserIdentity.user_id == owner.id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    return bound is not None


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
    total = effective_budget(prefs.daily_turn_budget, await default_budget(session))
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
    做完、所有者的账号停用或飞书身份解绑时不入队，条目留在箱里，这里不为此发任何私信。
    上一轮没走完之后的等待期内
    （`handover_retry_at`）同样不入队，新到的条目也等着。
    """
    prefs = await session.get(AssistantSettings, bot.id)
    if prefs is None or not gate(bot, prefs, now):
        return None
    row = await state(session, bot.id, lock=True)
    if row is None or await _in_flight(session, bot.id):
        return None
    if row.handover_retry_at is not None and aware_utc(row.handover_retry_at) > aware_utc(now):
        return None
    if await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id):
        return None
    # 运行时（实例停用、节点离线、没登录……）与所有者的状态都在入队前看：开轮时才发现的话，
    # 那一轮记一次安静的失败或取消，条目放回再排，周而复始。
    if await runtime.turn_blocker(session, bot) is not None:
        return None
    if not await _owner_ready(session, bot):
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


async def opened(session: AsyncSession, task: Task) -> int:
    """这一轮开了流：它带的条目各记一次「交回过」。与开流同一个事务，调用方提交。

    记的是模型看到过这一条。这一轮要是没走完（被所有者的消息打断、中途失败），条目回到待
    交回，下一轮的整理稿凭这个数在条目上写明「之前交回过」——被打断那一轮的转录不保证续得上
    （节点在后台上传，下一轮先开就换新会话），助理不能指望自己还记得做过什么。
    """
    done = await session.execute(
        update(AssistantInbox)
        .where(
            AssistantInbox.bot_id == task.bot_id,
            AssistantInbox.delivered_task_id == task.id,
            AssistantInbox.status == "pending",
        )
        .values(handovers=AssistantInbox.handovers + 1, updated_at=func.now())
    )
    return _count(done)


async def _settle(session: AsyncSession, task_id: int, ok: bool) -> int:
    unsettled = (AssistantInbox.delivered_task_id == task_id, AssistantInbox.status == "pending")
    # 先锁住这一轮还没记账的条目：重复调用（收尾与对账各来一次）时后到的那次拿不到行，
    # 不会把这一轮算两次。
    held = (
        await session.execute(
            select(AssistantInbox.id, AssistantInbox.bot_id).where(*unsettled).with_for_update()
        )
    ).all()
    if not held:
        return 0
    ids = [int(found[0]) for found in held]
    bot_id = held[0][1]
    mine = (AssistantInbox.id.in_(ids), *unsettled)
    pace = update(AssistantDelivery).where(AssistantDelivery.bot_id == bot_id)
    if ok:
        await session.execute(
            update(AssistantInbox).where(*mine).values(status="delivered", updated_at=func.now())
        )
        await session.execute(
            pace.values(handover_misses=0, handover_retry_at=None, updated_at=func.now())
        )
        return len(ids)
    ended = (
        await session.execute(select(Task.status, Task.error_code).where(Task.id == task_id))
    ).first()
    # 真失败：任务记了失败，原因又不是「没轮到」。取消、任务行已不在的都不算。
    failed = ended is not None and ended[0] == "failed" and ended[1] not in QUIET_CODES
    # 这位助理连续第几轮没走完：决定下一轮等多久。状态行不在（助理刚被删）时按第一次算。
    misses = await session.scalar(
        pace.values(
            handover_misses=AssistantDelivery.handover_misses + 1, updated_at=func.now()
        ).returning(AssistantDelivery.handover_misses)
    )
    wait = retry_delay(int(misses or 1))
    await session.execute(pace.values(handover_retry_at=func.now() + wait))
    retry = func.now() + (max(wait, RETRY_AFTER_FAILURE) if failed else wait)
    values: dict[str, Any] = {
        "delivered_task_id": None,
        "hold_until": func.greatest(func.coalesce(AssistantInbox.hold_until, retry), retry),
        "updated_at": func.now(),
    }
    if failed:
        values["failures"] = AssistantInbox.failures + 1
    await session.execute(update(AssistantInbox).where(*mine).values(**values))
    if failed:
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
            暂停（`assistant_paused`）、失败都一样，下一轮再带上；真失败另计次数。没走完的
            轮次连续出现时，下一轮的等待逐次加倍（`retry_delay`），走完一轮清零。
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


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime) -> dict[str, int]:
    """scheduler 每 5 秒调一次：先给已经结束的交回轮次记账，再给有到点条目的助理各排一轮。

    每位助理一个自己的事务；一位出错只记类型，不挡别的。
    """
    counts = {"assistant_inbox_settled": 0, "assistant_handovers": 0}
    async with factory() as session:
        counts["assistant_inbox_settled"] = await reconcile(session)
        await session.commit()
        waiting = list(
            await session.scalars(
                select(AssistantInbox.bot_id)
                .join(Bot, Bot.id == AssistantInbox.bot_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    AssistantInbox.status == "pending",
                    AssistantInbox.delivered_task_id.is_(None),
                    or_(AssistantInbox.hold_until.is_(None), AssistantInbox.hold_until <= now),
                )
                .distinct()
                .limit(TICK_BATCH)
            )
        )
    for bot_id in waiting:
        try:
            async with factory() as session:
                await sealed.guard_logs(session, bot_id)
                bot = await session.get(Bot, bot_id)
                task = await dispatch(session, bot, now) if bot is not None else None
                await session.commit()
        except Exception as exc:  # noqa: BLE001 一位助理出错不挡别的
            log.warning("assistant_delivery_failed", bot_id=str(bot_id), error=type(exc).__name__)
            continue
        counts["assistant_handovers"] += int(task is not None)
    return counts


@dataclass(frozen=True)
class Usage:
    """一位助理今天的轮次：已用与预算。"""

    used: int
    budget: int


async def usage(session: AsyncSession, now: datetime) -> dict[uuid.UUID, Usage]:
    """每位助理今天（各自所有者的时区）跑了几轮、预算多少；管理员的助理列表用。

    按时区分组数对话记录：查询条数等于用到的时区数，不随助理人数增长。只读元数据。
    """
    fallback = await default_budget(session)
    rows = (
        await session.execute(
            select(Bot.id, AssistantSettings.timezone, AssistantSettings.daily_turn_budget)
            .outerjoin(AssistantSettings, AssistantSettings.bot_id == Bot.id)
            .where(Bot.kind == "assistant")
        )
    ).all()
    zones: dict[str, list[uuid.UUID]] = {}
    for bot_id, name, _own in rows:
        zones.setdefault(zone_named(name).key, []).append(bot_id)
    used: dict[uuid.UUID, int] = {}
    for key, bot_ids in zones.items():
        zone = ZoneInfo(key)
        start, end = day_bounds(zone, aware_utc(now).astimezone(zone).date())
        counted = await session.execute(
            select(ChatLog.bot_id, func.count(ChatLog.id))
            .where(
                ChatLog.bot_id.in_(bot_ids), ChatLog.request_at >= start, ChatLog.request_at < end
            )
            .group_by(ChatLog.bot_id)
        )
        used.update({bot_id: int(count) for bot_id, count in counted.all()})
    return {
        bot_id: Usage(used.get(bot_id, 0), effective_budget(own, fallback))
        for bot_id, _name, own in rows
    }
