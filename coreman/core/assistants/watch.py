"""盯消息：平台以所有者的身份轮询飞书，把别人发给他的消息分级写进收件箱。不经模型。

scheduler 的 `_assistant_watch_loop` 每 5 秒调一次 `tick`。每位助理一个自己的短事务：认领
（`FOR UPDATE SKIP LOCKED`）、过门槛、列一次会话、读有新动静的会话、入箱、记下游标与下次
轮询的时刻，然后提交——刷新令牌的锁（`feishu_personal.service.lock` 与授权行的行锁）随之
释放，不会被别人的轮询占着。

一轮里调过飞书之后不回滚：刷新令牌一次性使用，这一轮里换到的新令牌必须落库。读到一半失败
（`PersonalError`）时，已入箱的条目与已读完的会话的游标照常提交，下一轮接着来；重读由游标
与收件箱的唯一索引去重。只有意外的异常才回滚，再另开事务把这一行往后推。

消息内容只在这里的内存里：出去的只有加密的摘要（`watch_inbox`）、加密的会话顺序与游标里的
编号和时间。日志只带代码与计数。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import followups, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants import watch_feed as feed
from coreman.core.assistants import watch_inbox as inbox
from coreman.core.assistants.records import is_assistant
from coreman.core.assistants.watch_grade import (
    HOLD_WINDOW,
    Context,
    Item,
    Level,
    grade,
    release_at,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantSettings,
    AssistantWatch,
    Bot,
    FeishuPersonalGrant,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc, utcnow

__all__ = ["Context", "Item", "Level", "grade", "owner_scope", "stop", "tick"]

log = get_logger(__name__)
# 轮询间隔（秒）；用量接近上限时成倍拉长，不超过 MAX_INTERVAL。
BASE_INTERVAL = 60
MAX_INTERVAL = 3600
# 一次 tick 最多认领多少位助理、同时轮询几位（scheduler 进程的连接池是 5 + 5）。
BATCH = 40
CONCURRENCY = 4
# 一轮最多读多少次历史、最多用多久。到点不取消正在进行的调用（取消一次正在进行的令牌刷新
# 会把刷新令牌用掉却拿不到新的），只是不再发起新的；没读完的会话下一轮先读。
MAX_READS = 12
ROUND_SECONDS = 30.0
# 顺序没变的会话从头逐个探，最多探这么多个。
CHAIN_MAX = 10
# 每轮直接读的重点群数。
FOCUS_MAX = 5
ERROR_BACKOFF_MAX = 900
# 同一个会话连续这么多轮读不了（飞书拒绝：没有权限、会话不让读）就先放下它，隔 PARK 再试：
# 一个读不了的会话不该让这位助理的整个轮询一直退避。
CHAT_STRIKES = 3
PARK = timedelta(hours=1)
# 刷新令牌约 7 天不用就失效：不轮询的助理每这么久强制刷新一次。
KEEPALIVE = timedelta(hours=24)


@dataclass
class Outcome:
    """一位助理这一次的结果，只供 `tick` 汇总。"""

    state: str
    items: int = 0
    kept_alive: bool = False
    app_id: str = ""
    load: float = 0.0


def phase(bot_id: uuid.UUID, interval: int = BASE_INTERVAL) -> int:
    """这位助理在一个间隔里的相位（秒）：各人不同，轮询时刻就错开了。"""
    return bot_id.int % interval


def slot(now: datetime, interval: int, offset: int) -> datetime:
    """`now` 之后、落在相位 `offset` 上的下一个时刻（最多一个间隔之后）。"""
    wait = (offset - int(now.timestamp())) % interval
    return now.replace(microsecond=0) + timedelta(seconds=wait or interval)


def _schedule(row: AssistantWatch, now: datetime, interval: int) -> None:
    row.interval = max(BASE_INTERVAL, min(MAX_INTERVAL, interval))
    row.next_poll_at = slot(now, row.interval, phase(row.bot_id, row.interval))


def _rest(row: AssistantWatch, now: datetime, state: str, code: str | None) -> Outcome:
    """这一轮不轮询，也没出错：过一个间隔再来看。"""
    row.state, row.last_error, row.failures = state, code, 0
    _schedule(row, now, BASE_INTERVAL)
    return Outcome(state)


def _failed(row: AssistantWatch, now: datetime, code: str, wait: int | None = None) -> Outcome:
    """这一轮出了错：记下代码，按连续失败次数退避（2、4、8 分钟，最多 15 分钟）。"""
    row.failures += 1
    row.state, row.last_error = "backoff", sealed.code_only(code)
    if wait is None:
        wait = min(ERROR_BACKOFF_MAX, BASE_INTERVAL * 2 ** min(row.failures, 4))
    row.next_poll_at = now + timedelta(seconds=wait)
    return Outcome("backoff")


async def owner_scope(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings
) -> tuple[Scope | None, str]:
    """以所有者身份调飞书用的核验结果；不成立时返回 (None, 原因代码)。

    给平台自己的循环用（盯消息、投递层顺带的检查）：没有任务、没有入站事件，身份取自开通时
    记下的设置。拿到的 scope 只能交给不读 `scope.task` 的调用（`toolbase.Call` 的读接口、
    `service.keep_alive`）。

    轮询循环里没有任务：`Scope.task` 照 `provisioning._grant_scope` 的做法留空，调用链
    （`Call` → `service.api_request` / `keep_alive`）不读它。身份取自开通时记下的设置，
    并照 `platform_turns.scope_for` 的做法先核对：授权行与记下的身份对不上时在这里拒绝，
    不交给 `service._locked`——它遇到不一致会清空令牌，一条写错的设置不该把授权清掉。
    """
    owner = await session.get(User, bot.owner_id, populate_existing=True) if bot.owner_id else None
    if owner is None or owner.status != "active" or owner.source == "bootstrap":
        return None, "personal_permission_revoked"
    bound = await session.scalar(
        select(UserIdentity.user_id)
        .where(
            UserIdentity.user_id == owner.id,
            UserIdentity.platform == "feishu",
            UserIdentity.platform_user_id == prefs.platform_user_id,
        )
        .limit(1)
    )
    if bound is None:
        return None, "personal_grant_required"
    grant = await session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    if grant is None or grant.status != "connected" or not grant.token_enc:
        return None, "authorization_required"
    if (grant.platform_user_id, grant.open_id, grant.tenant_key) != (
        prefs.platform_user_id,
        prefs.open_id,
        prefs.tenant_key,
    ):
        return None, "personal_grant_required"
    scope = Scope(
        cast(Task, None),
        bot,
        owner.id,
        prefs.tenant_key,
        grant.app_id,
        prefs.open_id,
        prefs.platform_user_id,
        prefs.chat_id,
        platform=True,
    )
    return scope, ""


async def _resting(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, now: datetime
) -> tuple[str, str | None] | None:
    """这位助理现在为什么不轮询：(状态, 代码)；该轮询返回 None。"""
    if prefs.cold_start_done_at is None:
        return "idle", "cold_start_pending"
    if not assistant_settings.on_shift(prefs, now):
        return "idle", None
    blocker = await runtime.turn_blocker(session, bot)
    return ("paused", blocker) if blocker is not None else None


async def _keep_alive(
    session: AsyncSession, cipher: Cipher, scope: Scope, row: AssistantWatch, now: datetime
) -> bool:
    """不轮询的这段时间里把令牌续上；距上次确认它还活着不满 `KEEPALIVE` 就什么都不做。

    轮询成功也算确认过（轮询期间访问令牌每两小时过期，自然会刷新）。从没确认过的从这一行
    建出来的时刻算起。
    """
    last = row.keepalive_at or row.created_at
    if aware_utc(last) + KEEPALIVE > now:
        return False
    await personal.keep_alive(session, cipher, scope)
    row.keepalive_at = now
    return True


@dataclass
class _Round:
    """一位助理的一轮。读到的消息只在这里的内存里。"""

    session: AsyncSession
    call: feed.Reader
    bot_id: uuid.UUID
    prefs: AssistantSettings
    now: datetime
    listed: feed.Listed
    cursor: dict[str, Any]
    deadline: float
    reads: int = 0
    items: int = 0
    # 缺编号或读不出时间、没法处理的消息数：只记个数。
    skipped: int = 0
    # 正在读的会话：读失败时记到它头上。
    reading: str = ""
    # 这一轮读到了页数上限、后面还有消息的会话：下一轮接着读。
    more: list[str] = field(default_factory=list)

    def spent(self) -> bool:
        return self.reads >= MAX_READS or time.monotonic() >= self.deadline

    async def recall(self, message_id: str) -> None:
        """对方撤回了这条消息：还没交回的条目作废，由它引起的待跟进自动关闭。"""
        voided = await inbox.void_message(self.session, self.bot_id, message_id, "recalled")
        closed = await followups.close_for_message(self.session, self.bot_id, message_id)
        if voided or closed:
            log.info(
                "assistant_watch_recalled",
                bot_id=str(self.bot_id),
                voided=voided,
                followups=len(closed),
            )

    async def take(self, item: Item, entry: dict[str, Any]) -> None:
        """处理游标之后的一条消息：撤回的作废，所有者自己发的压住这个会话，其余分级入箱。"""
        owner = self.prefs.open_id
        if item.deleted:
            await self.recall(item.message_id)
            return
        if item.sender_type == "user" and item.sender_id == owner:
            entry["o"] = max(int(entry.get("o") or 0), item.at_ms)
            await inbox.hold_chat(
                self.session, self.bot_id, item.chat_id, item.created_at + HOLD_WINDOW
            )
            return
        spoke = int(entry.get("o") or 0)
        context = Context(
            now=self.now,
            owner_spoke_at=datetime.fromtimestamp(spoke / 1000, UTC) if spoke else None,
        )
        level = grade(item, self.prefs, context)
        if level is Level.IGNORE:
            return
        batch_until = (
            await inbox.batch_until(self.session, self.bot_id, self.now)
            if level is Level.BATCH
            else None
        )
        added = await inbox.add(
            self.session,
            self.bot_id,
            item,
            level,
            owner_open_id=owner,
            hold_until=release_at(level, context, batch_until=batch_until),
        )
        self.items += int(added)

    async def chat(self, chat_id: str) -> bool:
        """读一个会话游标之后的消息（主线，再加跟着的话题）；返回有没有新消息。

        游标（`t`、`m`、`th`）在整个会话读完之后才写回 `entry`：读到一半失败，这个会话下一轮
        从原来的位置重读。
        """
        entry: dict[str, Any] = self.cursor["chats"].setdefault(chat_id, {})
        self.reading = chat_id
        last = int(entry.get("t") or self.cursor["since"])
        same = [str(one) for one in entry.get("m") or []]
        kind = entry.get("k") or self.listed.kinds.get(chat_id)
        name = self.listed.names.get(chat_id, "")
        if kind is None:
            kind, fetched = await feed.chat_kind(self.call, chat_id)
            name = name or fetched
        entry["k"] = kind
        raws, calls = await feed.read_main(self.call, chat_id, last)
        self.reads += calls
        if len(raws) >= feed.PAGE * feed.MAX_PAGES:
            self.more.append(chat_id)
        threads = {str(key): int(value) for key, value in (entry.get("th") or {}).items()}
        new = False
        for raw in raws:
            item = feed.to_item(raw, chat_id=chat_id, chat_kind=kind, chat_name=name)
            if item is None:
                self.skipped += 1
                continue
            if not feed.fresh(last, same, item):
                continue
            new = True
            if item.at_ms > last:
                last, same = item.at_ms, []
            same.append(item.message_id)
            await self.take(item, entry)
            if item.thread_id:
                threads[item.thread_id] = max(threads.get(item.thread_id, 0), item.at_ms)
        followed = sorted(threads, key=lambda key: threads[key], reverse=True)
        followed = followed[: feed.THREADS_TRACKED]
        for thread_id in followed:
            if self.spent():
                break
            seen = threads[thread_id]
            self.reads += 1
            for raw in await feed.read_thread(self.call, thread_id):
                item = feed.to_item(
                    raw, chat_id=chat_id, chat_kind=kind, chat_name=name, thread_id=thread_id
                )
                if item is None:
                    self.skipped += 1
                    continue
                if item.at_ms <= seen:
                    continue
                new = True
                threads[thread_id] = max(threads[thread_id], item.at_ms)
                await self.take(item, entry)
        entry["t"], entry["m"] = last, same
        entry["th"] = {thread_id: threads[thread_id] for thread_id in followed}
        # 读成了：之前记的读失败次数作废。
        entry.pop("f", None)
        entry.pop("x", None)
        self.reading = ""
        return new

    def strike(self, code: str) -> None:
        """正在读的会话被飞书拒绝了：记一次；连续 `CHAT_STRIKES` 次就放下它一段时间。

        只算飞书对这次读取的拒绝（`feishu_read_failed`）；授权失效、限频、上游不可用与哪个
        会话无关，不记。
        """
        entry = self.cursor["chats"].get(self.reading)
        if code != "feishu_read_failed" or entry is None:
            return
        entry["f"] = int(entry.get("f") or 0) + 1
        if entry["f"] >= CHAT_STRIKES:
            entry["f"] = CHAT_STRIKES - 1
            entry["x"] = int((self.now + PARK).timestamp() * 1000)
            log.warning("assistant_watch_chat_parked", bot_id=str(self.bot_id))


async def _round(
    session: AsyncSession,
    call: feed.Reader,
    row: AssistantWatch,
    prefs: AssistantSettings,
    now: datetime,
) -> int:
    """轮询一轮，返回新入箱的条数。做法见 spec 第五节与实现计划「设计选择」1–11。"""
    stored = dict(row.cursor or {})
    listed = await feed.list_chats(call)
    if not isinstance(stored.get("since"), int):
        # 第一轮只记基线：从现在起盯，不把之前的历史倒进收件箱。
        row.cursor = {"since": int(now.timestamp() * 1000), "chats": {}, "todo": []}
        row.head_enc = listed.order
        return 0
    cursor = feed.load(stored)
    # 所有者与助理自己的私聊、他设为忽略的会话：不读，也不打断下面的逐个探测。
    skip = {prefs.chat_id, *(prefs.ignored_chats_enc or [])}
    # 连续读不了、先放下的会话，到点之前同样对待。
    now_ms = int(now.timestamp() * 1000)
    skip |= {chat for chat, entry in cursor["chats"].items() if int(entry.get("x") or 0) > now_ms}
    focus = list(prefs.focus_chats_enc or [])[:FOCUS_MAX]
    moved, rest = feed.split(listed.order, list(row.head_enc or []))
    # 上一轮没读完的、这一轮排到前面的、重点群：都读。
    queue = [chat for chat in dict.fromkeys([*cursor["todo"], *moved, *focus]) if chat not in skip]
    state = _Round(
        session, call, row.bot_id, prefs, now, listed, cursor, time.monotonic() + ROUND_SECONDS
    )
    todo: list[str] = []
    try:
        for chat_id in queue:
            if state.spent():
                todo.append(chat_id)
            else:
                await state.chat(chat_id)
        # 顺序没变的会话里，藏着新消息的只可能是开头连续的一段：从头探，探到没有新消息的为止。
        probes = 0
        for chat_id in rest:
            if chat_id in skip or chat_id in queue:
                continue
            if state.spent() or probes >= CHAIN_MAX:
                break
            probes += 1
            if not await state.chat(chat_id):
                break
        if time.monotonic() < state.deadline:
            waiting = await inbox.pending_messages(session, row.bot_id)
            for message_id in await feed.recalled(call, waiting):
                await state.recall(message_id)
    except PersonalError as exc:
        # 读到一半失败：已读完的会话的进度留下，顺序不存，下一轮重新比。
        state.strike(exc.code)
        row.cursor = feed.dump(cursor, cursor["todo"])
        raise
    finally:
        if state.skipped:
            log.info(
                "assistant_watch_messages_skipped", bot_id=str(row.bot_id), count=state.skipped
            )
    row.cursor = feed.dump(cursor, list(dict.fromkeys([*todo, *state.more])))
    row.head_enc = listed.order
    return state.items


async def _step(
    session: AsyncSession, cipher: Cipher, row: AssistantWatch, now: datetime
) -> Outcome:
    """认领到的一位助理：过门槛，轮询一轮，记下结果与下次的时刻。"""
    bot = await session.get(Bot, row.bot_id, populate_existing=True)
    if bot is None or not is_assistant(bot) or not bot.enabled or bot.assistant_state != "active":
        return _rest(row, now, "idle", None)
    prefs = await assistant_settings.get(session, bot.id)
    if not assistant_settings.identified(prefs):
        return _rest(row, now, "idle", "identity_missing")
    scope, code = await owner_scope(session, bot, prefs)
    if scope is None:
        return _failed(row, now, code)
    resting = await _resting(session, bot, prefs, now)
    if resting is not None:
        # 下班、冷启动没做完、运行时暂停：不轮询，但授权不能因此过期。
        kept = await _keep_alive(session, cipher, scope, row, now)
        outcome = _rest(row, now, *resting)
        outcome.kept_alive = kept
        return outcome
    # 飞书按应用限频，全员共用：先看这个应用最近一分钟用了多少。
    budget = ratelimit.Budget(scope.app_id)
    load = await budget.load(session, now)
    factor = ratelimit.stretch(load)
    if factor is None:
        # 额度已满（或有人刚被限频）：这一轮一次都不调，过一个间隔再看。不算这位助理的失败。
        row.state, row.last_error = "backoff", "budget_exhausted"
        _schedule(row, now, BASE_INTERVAL)
        return Outcome("backoff", app_id=scope.app_id, load=load)
    try:
        items = await _round(session, Call(session, cipher, scope), row, prefs, now)
    except ratelimit.RateLimited as exc:
        if exc.category is not None:
            # 记一笔占满额度、等待结束才滑出窗口的用量：同一应用的人这段时间都不调。
            await budget.exhaust(session, exc.category, now, exc.retry_after)
        log.warning(
            "assistant_watch_rate_limited",
            bot_id=str(row.bot_id),
            category=exc.category,
            retry_after=exc.retry_after,
        )
        outcome = _failed(row, now, exc.code, wait=exc.retry_after)
        outcome.app_id, outcome.load = scope.app_id, 1.0
        return outcome
    row.state, row.last_error, row.failures = "polling", None, 0
    row.last_ok_at = row.keepalive_at = now
    # 接近上限时拉长间隔：每个人都按同一个用量算，所以是所有人一起变慢。
    _schedule(row, now, BASE_INTERVAL * factor)
    return Outcome("polling", items=items, app_id=scope.app_id, load=load)


async def _visit(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, bot_id: uuid.UUID, now: datetime
) -> Outcome:
    """一位助理一个事务。被别人认领着、或已经不到期的跳过。"""
    async with factory() as session:
        await sealed.guard_logs(session, bot_id)
        # 这个会话只处理这一位助理：上一轮的顺序解不开时按没有处理，不让它卡住这一行。
        sealed.tolerate_undecryptable(session)
        row = await session.scalar(
            select(AssistantWatch)
            .where(
                AssistantWatch.bot_id == bot_id,
                AssistantWatch.state != "stopped",
                AssistantWatch.next_poll_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return Outcome("skipped")
        try:
            outcome = await _step(session, cipher, row, now)
        except PersonalError as exc:
            # 不回滚：这一轮里刷新过的令牌、已入箱的条目照常提交。刷新令牌时被限频的
            # （不属于哪一类接口）也到这里，按它给的秒数等。
            wait = exc.retry_after if isinstance(exc, ratelimit.RateLimited) else None
            outcome = _failed(row, now, exc.code, wait=wait)
        except Exception as exc:  # noqa: BLE001 只记类型
            # 意外出错也先试着提交：这一轮里可能已经刷新过令牌（旧的刷新令牌已经用掉），回滚
            # 会让授权就此作废。会话本身坏了（数据库错误）时提交会再抛，由 `tick` 另开事务
            # 把这一行往后推。
            log.warning("assistant_watch_failed", bot_id=str(bot_id), error=type(exc).__name__)
            _failed(row, now, type(exc).__name__, wait=ERROR_BACKOFF_MAX)
            outcome = Outcome("failed")
        row.updated_at = now
        await session.commit()
        return outcome


async def _push_back(
    factory: async_sessionmaker[AsyncSession], bot_id: uuid.UUID, code: str, now: datetime
) -> None:
    """`_visit` 连提交都没成（事务已回滚）：另开事务把这一行往后推，免得它每 5 秒重来一次。"""
    try:
        async with factory() as session:
            await session.execute(
                update(AssistantWatch)
                .where(AssistantWatch.bot_id == bot_id, AssistantWatch.state != "stopped")
                .values(
                    state="backoff",
                    last_error=sealed.code_only(code),
                    failures=AssistantWatch.failures + 1,
                    next_poll_at=now + timedelta(seconds=ERROR_BACKOFF_MAX),
                    updated_at=now,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 兜底本身出错也不能让循环停下
        log.warning("assistant_watch_push_back_failed", error=type(exc).__name__)


async def _enroll(session: AsyncSession, now: datetime) -> int:
    """给启用中的 active 助理补上盯消息的行；第一次轮询落在各自的相位上。"""
    missing = list(
        await session.scalars(
            select(Bot.id)
            .outerjoin(AssistantWatch, AssistantWatch.bot_id == Bot.id)
            .where(
                Bot.kind == "assistant",
                Bot.enabled.is_(True),
                Bot.assistant_state == "active",
                AssistantWatch.bot_id.is_(None),
            )
            .limit(200)
        )
    )
    if not missing:
        return 0
    await session.execute(
        insert(AssistantWatch)
        .values(
            [
                {"bot_id": bot_id, "next_poll_at": slot(now, BASE_INTERVAL, phase(bot_id))}
                for bot_id in missing
            ]
        )
        .on_conflict_do_nothing(index_elements=[AssistantWatch.bot_id])
    )
    return len(missing)


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """登记缺行的助理，认领到期的各轮询一次；返回这一次的计数。"""
    now = aware_utc(now)
    async with factory() as session:
        enrolled = await _enroll(session, now)
        due = list(
            await session.scalars(
                select(AssistantWatch.bot_id)
                .where(AssistantWatch.state != "stopped", AssistantWatch.next_poll_at <= now)
                .order_by(AssistantWatch.next_poll_at, AssistantWatch.bot_id)
                .limit(BATCH)
            )
        )
        await session.commit()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(bot_id: uuid.UUID) -> Outcome:
        async with gate:
            try:
                return await _visit(factory, cipher, bot_id, now)
            except Exception as exc:  # noqa: BLE001 一位出错不挡别人；只记类型
                log.warning("assistant_watch_failed", bot_id=str(bot_id), error=type(exc).__name__)
                await _push_back(factory, bot_id, type(exc).__name__, now)
                return Outcome("failed")

    outcomes = await asyncio.gather(*(one(bot_id) for bot_id in due))
    counts = {
        "assistant_watch_enrolled": enrolled,
        "assistant_watch_polled": 0,
        "assistant_watch_items": 0,
        "assistant_watch_backoff": 0,
        "assistant_watch_failed": 0,
        "assistant_watch_keepalives": 0,
    }
    for outcome in outcomes:
        if outcome.state == "polling":
            counts["assistant_watch_polled"] += 1
        elif outcome.state in ("backoff", "failed"):
            counts[f"assistant_watch_{outcome.state}"] += 1
        counts["assistant_watch_items"] += outcome.items
        counts["assistant_watch_keepalives"] += int(outcome.kept_alive)
    tight: dict[str, float] = {}
    for outcome in outcomes:
        if outcome.app_id and outcome.load >= ratelimit.TIGHT:
            tight[outcome.app_id] = max(tight.get(outcome.app_id, 0.0), outcome.load)
    for app_id, load in tight.items():
        # 每次 tick 每个应用一条：人数多到一个应用撑不住时，这条日志会持续出现。
        log.warning(
            "assistant_watch_budget_tight",
            app_id=app_id,
            load=round(load, 2),
            stretch=ratelimit.stretch(load) or 0,
        )
    return counts


async def stop(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """这位助理不再被盯：停用流程在叫停任务之前调用。幂等；调用方提交。

    状态改成 `stopped`（`tick` 不认领、也不会重新登记），游标与保存的会话顺序清掉，还没交回
    的条目作废。正在进行的一轮持有这一行的锁：这里等它提交，它入箱的条目随后一并作废。
    """
    row = await session.get(AssistantWatch, bot_id, with_for_update=True)
    if row is not None and row.state != "stopped":
        row.state, row.last_error, row.failures = "stopped", None, 0
        row.cursor, row.head_enc = {}, None
        row.updated_at = utcnow()
    await inbox.void_all(session, bot_id, "stopped")
