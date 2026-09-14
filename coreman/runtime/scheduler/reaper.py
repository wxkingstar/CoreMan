"""看护动作：掉线任务收尸 + 过期行清理（spec §4.1、§6.2、§6.5）。

每个动作都是一条独立的函数：接收 `session` 与 `now`、返回处理条数、**不 commit**——事务边界
交给调用方（主循环一个动作一个短事务，用例则直接在自己的会话里断言）。`now` 由调用方传进来
而不是各自取 `now()`：一轮看护里的所有判定必须用同一个时间基准，否则「先判租约过期、后判实例
死亡」会在秒边界上互相打架，用例也没法稳定复现。

这里是唯一直接对总线表做批量 DML 的地方（`coreman/runtime/bus` 的函数管单行语义）：收尸与
GC 天然是集合操作，一条 `delete/update ... RETURNING` 比「查出来再逐行调 bus」便宜一个数量级；
带业务语义的那部分（任务落 failed、流补终态、出站入队）仍然走 bus，通知才不会漏发。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable

from coreman.core.chat import interactions
from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.db.models import (
    CHAT_TYPES,
    Bot,
    BotLease,
    ChatSession,
    InteractionState,
    OutboxItem,
    ProcessInstance,
    Task,
    TaskStream,
)
from coreman.core.i18n.messages import msg
from coreman.core.knowledge.installation import recover_installations
from coreman.core.settings_schema import SETTING_DEFAULTS
from coreman.core.settings_store import SettingsStore
from coreman.runtime.bus import leases, outbox, streams, tasks
from coreman.runtime.bus.notify import notify
from coreman.runtime.bus.tasks import ACTIVE

# worker 每 10 秒写一次任务心跳，60 秒一次都没写上来就当这个 worker 没了（spec §6.2）。
TASK_TIMEOUT_SECONDS = 60
# 网关租约的过期判据与网关自己抢占时用的完全同源，否则会出现「谁都不认为对方死了」的僵局。
LEASE_STALE_SECONDS = leases.STALE_AFTER_SECONDS
INSTANCE_DEAD_SECONDS = 60
INSTANCE_PURGE_DAYS = 7
STREAM_RETENTION_HOURS = 1
OUTBOX_RETENTION_DAYS = 7
LOST_ERROR_CODE = "worker_lost"
LOST_ERROR_MESSAGE = "worker 心跳超时"

Job = Callable[[AsyncSession, datetime], Awaitable[int]]


async def _affected(session: AsyncSession, stmt: Executable) -> int:
    """跑一条带 RETURNING 的批量 DML，返回命中条数。

    `synchronize_session=False`：这些行要么马上没了，要么调用方会用 `populate_existing`
    取回，让 ORM 再去补一次同步查询纯属浪费。
    """
    rows = (await session.execute(stmt, execution_options={"synchronize_session": False})).all()
    return len(rows)


# ---- 收尸 -----------------------------------------------------------------


async def reap_lost_tasks(
    session: AsyncSession,
    now: datetime,
    *,
    timeout_seconds: int = TASK_TIMEOUT_SECONDS,
    chat_logs_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """worker 掉线（进程被杀、机器宕、网络割裂）后替它把手里的任务收尾。

    三件事缺一不可，少一件用户那边就是永远转圈：① 任务落 `failed/worker_lost`；② 流补一个
    终态，网关据此把最后一段推出去；③ 推不动流的（proactive、或 finish 已经推过）改走出站箱，
    `dedupe_key` 保证同一个任务反复收尸也只会发出去一条。

    `claimed` 同样要收：worker 认领之后、`start` 之前被杀（或 `start` 就没写成），这一行
    再也不会有人动它——只看 `running` 的话它会一直挂在在途列表里，用户那边永远转圈。
    """
    cutoff = now - timedelta(seconds=timeout_seconds)
    # claimed 行可能还没写过 heartbeat_at，用认领时刻兜底。
    beat = func.coalesce(Task.heartbeat_at, Task.claimed_at)
    stmt = (
        select(Task)
        .where(Task.status.in_(ACTIVE), beat < cutoff)
        .order_by(Task.id)
        .limit(500)
        .with_for_update(skip_locked=True)
    )
    lost = list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )
    writer = None if chat_logs_factory is None else ChatLogWriter(chat_logs_factory)
    for task in lost:
        bot = await session.get(Bot, task.bot_id)
        stream = await streams.get(session, task.id)
        # 日志先攒好：finish 之后 task 的属性会被过期掉，再读要多打一次 SELECT。
        entry = (
            None
            if writer is None or task.kind in {"cron_run", "escalation_media", "skill_install"}
            else _lost_entry(task, stream, bot, now)
        )
        await tasks.finish(
            session,
            task.id,
            status="failed",
            error_code=LOST_ERROR_CODE,
            error_message=LOST_ERROR_MESSAGE,
        )
        if task.kind == "choice_submit":
            try:
                state_id = uuid.UUID(str(task.payload.get("state_id")))
            except (ValueError, TypeError):
                pass
            else:
                await session.execute(
                    delete(InteractionState).where(
                        InteractionState.id == state_id, InteractionState.status == "submitted"
                    )
                )
        await _tell_user(session, task, stream, bot)
        if writer is not None and entry is not None:
            await writer.write(entry)
    return len(lost)


async def recover_terminal_streams(session: AsyncSession, now: datetime) -> int:
    """补偿任务终态提交后、流终态提交前崩溃的窗口；不重跑模型或改变任务结果。"""
    rows = list(
        await session.execute(
            select(Task, TaskStream)
            .join(TaskStream, TaskStream.task_id == Task.id)
            .where(
                Task.status.in_(("succeeded", "failed", "cancelled", "timed_out")),
                Task.finished_at < now - timedelta(seconds=60),
                TaskStream.is_complete.is_(False),
            )
            .order_by(Task.id)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    )
    for task, stream in rows:
        final = stream.final_text or stream.pending_text or msg("stream_recovered")
        if task.status != "succeeded":
            final += "\n\n" + msg("stream_interrupted")
        await streams.complete(session, task.id, final_text=final)
        if stream.delivery_mode != "stream" or stream.finish_pushed_at is not None:
            bot = await session.get(Bot, task.bot_id)
            chat_id = stream.reply_context.get("chat_id") or _chat_id(task)
            if bot and chat_id:
                await outbox.add(
                    session,
                    bot_id=bot.id,
                    platform=bot.platform,
                    kind="send",
                    dedupe_key=f"{task.id}:terminal-recovery",
                    target={"chat_id": str(chat_id)},
                    payload={"markdown": final},
                )
    return len(rows)


async def _tell_user(
    session: AsyncSession, task: Task, stream: TaskStream | None, bot: Bot | None
) -> None:
    """把「进程没了」告诉用户：能补流就补流，补不了就主动发一条。"""
    lost = msg(LOST_ERROR_CODE)
    # 只有还在推的流式回复才能靠 finish 送达；proactive 与 finish 已推的流，网关不会再碰它。
    pushable = (
        stream is not None
        and not stream.is_complete
        and stream.delivery_mode == "stream"
        and stream.finish_pushed_at is None
    )
    if stream is not None and not stream.is_complete:
        # 推得出去就把已经写了一半的正文带上，用户至少看得到中断前的内容。
        prefix = f"{stream.pending_text}\n\n" if pushable and stream.pending_text else ""
        await streams.complete(session, task.id, final_text=prefix + lost)
    if pushable:
        return
    chat_id = _chat_id(task)
    if not chat_id:
        return
    await outbox.add(
        session,
        bot_id=task.bot_id,
        platform=bot.platform if bot is not None else str(_message(task).get("platform") or ""),
        kind="send",
        dedupe_key=f"{task.id}:send:lost",
        target={"chat_id": chat_id},
        payload={"markdown": lost},
    )


def _message(task: Task) -> dict[str, Any]:
    payload: dict[str, Any] = task.payload or {}
    message = payload.get("message")
    return message if isinstance(message, dict) else {}


def _chat_id(task: Task) -> str:
    return str(_message(task).get("chat_id") or task.session_key or "")


def _lost_entry(
    task: Task, stream: TaskStream | None, bot: Bot | None, now: datetime
) -> ChatLogEntry:
    """收尸也要留一条 `timeout` 日志：管理台的失败率统计不能因为进程没了就少算一笔。"""
    payload: dict[str, Any] = task.payload or {}
    message = _message(task)
    chat_type = str(message.get("chat_type") or "single")
    request_at = task.claimed_at or task.run_after
    return ChatLogEntry(
        bot_id=task.bot_id,
        bot_key=str(bot.bot_key if bot is not None else payload.get("bot_key") or ""),
        platform=str(bot.platform if bot is not None else message.get("platform") or ""),
        chat_type=chat_type if chat_type in CHAT_TYPES else "single",
        message_type="text",
        status="timeout",
        request_at=request_at,
        user_id=task.user_id,
        platform_user_id=_text_or_none(payload.get("platform_user_id")),
        chat_id=_chat_id(task) or None,
        session_key=task.session_key or _text_or_none(message.get("session_key")),
        model=bot.model if bot is not None else None,
        stream_id=stream.stream_id if stream is not None else None,
        task_id=task.id,
        error_code=LOST_ERROR_CODE,
        error_message=LOST_ERROR_MESSAGE,
        latency_ms=int((now - request_at).total_seconds() * 1000),
    )


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


# ---- 过期清理 --------------------------------------------------------------


async def release_stale_leases(session: AsyncSession, now: datetime) -> int:
    """持有者心跳过期的租约就地释放，并按 bot 发 `lease_changed`。

    网关自己抢占时也会绕过过期的持有者，这里多做一步是为了让**别的**网关立刻被叫醒，而不是
    等到它下一轮兜底扫描；顺带把 `connection_state` 落成 disconnected，管理台不再显示假在线。
    """
    cutoff = now - timedelta(seconds=LEASE_STALE_SECONDS)
    stmt = (
        update(BotLease)
        .where(BotLease.holder_instance.is_not(None), BotLease.heartbeat_at < cutoff)
        .values(holder_instance=None, released_at=now, connection_state="disconnected")
        .returning(BotLease.bot_id)
    )
    rows = (await session.execute(stmt, execution_options={"synchronize_session": False})).all()
    for row in rows:
        await notify(session, "lease_changed", {"bot_id": str(row[0])})
    return len(rows)


async def mark_dead_instances(session: AsyncSession, now: datetime) -> int:
    """心跳断了 60 秒的实例标记为已停止：进程被 kill -9 时没人来写 stopped_at。"""
    cutoff = now - timedelta(seconds=INSTANCE_DEAD_SECONDS)
    stmt = (
        update(ProcessInstance)
        .where(ProcessInstance.stopped_at.is_(None), ProcessInstance.heartbeat_at < cutoff)
        .values(stopped_at=now)
        .returning(ProcessInstance.id)
    )
    return await _affected(session, stmt)


async def purge_instances(session: AsyncSession, now: datetime) -> int:
    """停掉超过 7 天的实例行删掉（外键都是 SET NULL，删它不会带走任务与租约）。"""
    cutoff = now - timedelta(days=INSTANCE_PURGE_DAYS)
    stmt = (
        delete(ProcessInstance)
        .where(ProcessInstance.stopped_at.is_not(None), ProcessInstance.stopped_at < cutoff)
        .returning(ProcessInstance.id)
    )
    return await _affected(session, stmt)


async def cleanup_streams(session: AsyncSession, now: datetime) -> int:
    """完成 1 小时以上的流删掉：网关掉线补推的窗口早过了，留着只是拖慢活跃流的扫描。"""
    cutoff = now - timedelta(hours=STREAM_RETENTION_HOURS)
    stmt = (
        delete(TaskStream)
        .where(TaskStream.is_complete.is_(True), TaskStream.completed_at < cutoff)
        .returning(TaskStream.task_id)
    )
    return await _affected(session, stmt)


async def cleanup_outbox(session: AsyncSession, now: datetime) -> int:
    """发出去 7 天的出站条目删掉。失败 / 跳过的一律保留：那是要人来看的。"""
    cutoff = now - timedelta(days=OUTBOX_RETENTION_DAYS)
    stmt = (
        delete(OutboxItem)
        .where(OutboxItem.status == "sent", OutboxItem.sent_at < cutoff)
        .returning(OutboxItem.id)
    )
    return await _affected(session, stmt)


async def cleanup_sessions(session: AsyncSession, now: datetime, ttl_hours: int) -> int:
    """超过 TTL 没说过话的会话映射删掉，下次再聊就是一轮全新的 relay 会话（spec §8.5）。"""
    cutoff = now - timedelta(hours=ttl_hours)
    stmt = (
        delete(ChatSession)
        .where(ChatSession.last_active_at < cutoff)
        .returning(ChatSession.session_key)
    )
    return await _affected(session, stmt)


# ---- 编排 -----------------------------------------------------------------


async def _run(factory: async_sessionmaker[AsyncSession], job: Job, now: datetime) -> int:
    """一个动作一个短事务：某一项卡住或报错，不能把同一轮里的其它项一起拖下水。"""
    async with factory() as session:
        affected = await job(session, now)
        await session.commit()
        return affected


async def run_tick(
    factory: async_sessionmaker[AsyncSession],
    now: datetime,
    *,
    chat_logs_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, int]:
    """高频那一档：收尸、放掉死网关的租约、标记死实例——都是「有人在等」的事。"""
    reap: Job = partial(reap_lost_tasks, chat_logs_factory=chat_logs_factory)
    return {
        "lost_tasks": await _run(factory, reap, now),
        "terminal_streams_recovered": await _run(factory, recover_terminal_streams, now),
        "skill_installations_recovered": await _run(factory, recover_installations, now),
        "stale_leases": await _run(factory, release_stale_leases, now),
        "dead_instances": await _run(factory, mark_dead_instances, now),
    }


async def run_cleanup(
    factory: async_sessionmaker[AsyncSession], store: SettingsStore, now: datetime
) -> dict[str, int]:
    """低频那一档：纯 GC，晚做一分钟没人会察觉。"""
    ttl = int(await store.get("session_ttl_hours", SETTING_DEFAULTS["session_ttl_hours"]))
    sessions: Job = partial(cleanup_sessions, ttl_hours=ttl)
    return {
        "old_streams": await _run(factory, cleanup_streams, now),
        "old_outbox": await _run(factory, cleanup_outbox, now),
        "old_sessions": await _run(factory, sessions, now),
        "purged_instances": await _run(factory, purge_instances, now),
        # 待答状态到点置 expired。读路径（get_open）本来就按 expires_at 过滤，不靠这一步才正确；
        # 但卡片回调是按 task_id 前缀反查的，那条路没有时间条件，得靠 status 认出「这轮已经过期」。
        "interactions_expired": await _run(factory, interactions.expire_due, now),
    }


async def run_all(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    now: datetime,
    *,
    chat_logs_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, int]:
    """跑完两档。主循环按各自节奏分开调，手动收尸（运维脚本、用例）用这个一把梭。"""
    counts = await run_tick(factory, now, chat_logs_factory=chat_logs_factory)
    counts.update(await run_cleanup(factory, store, now))
    return counts
