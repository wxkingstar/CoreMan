"""对话日志落库。

一轮对话一行。开流时在对话事务里写入 `running` 的一行（`open_turn`），会话查看链接一发出去，
查看页就能从这一行判断会话归属；收尾时在抢终态的同一事务里把它改成终态（`finish_turn`），
任务结束与记录结束一起提交。只知道结局、不知道内容的收尾（收尸、处理器崩溃）用
`close_running`，保留开流时写下的内容。

写日志永远不能弄挂一轮对话：这几处都先把调用方已有的改动 flush 出去（那些改动出错照常抛给
调用方），再在调用方事务里开一个保存点，写失败只回滚保存点、记一条告警、返回 False，调用方的
事务照常提交。收尾没写成的交给 `ChatLogWriter` 事后补写；开流没写成的，收尾时照样补一行。
开流前就结束的轮次（relay 不可用等）没有进行中的行，也由 `ChatLogWriter` 发射后不管地补一行，
它自己吞掉所有异常，积压到上限就丢弃并计数。
日志内容（用户消息、模型回复）只进库、绝不进日志流。
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, cast, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from coreman.core.db.models import CHAT_LOG_RUNNING, ChatLog, UserReached
from coreman.core.logging import get_logger
from coreman.core.pricing import estimate

log = get_logger(__name__)

# 超长内容截断：日志是给人看的审计痕迹，不是对话存档，relay 那边自有全量历史。
LIMITS: dict[str, int] = {
    "message_content": 10000,
    "quoted_content": 5000,
    "response_content": 50000,
    "error_message": 5000,
}
# 丢弃告警的抽样间隔：库挂了会连着丢几千条，每条打一行日志只会把磁盘一起打满。
_DROP_LOG_EVERY = 100
_RETRY_DELAYS = (1.0, 3.0)
_MAX_LATENCY_MS = 2**31 - 1


@dataclass
class ChatLogEntry:
    """chat_logs 的一行（除 id / created_at 外的全部可写列）。"""

    bot_id: uuid.UUID
    bot_key: str
    platform: str
    chat_type: str
    message_type: str
    status: str
    request_at: datetime
    user_id: uuid.UUID | None = None
    platform_user_id: str | None = None
    user_login: str | None = None
    user_name: str | None = None
    chat_id: str | None = None
    session_key: str | None = None
    relay_session_id: uuid.UUID | None = None
    relay_server_id: uuid.UUID | None = None
    model: str | None = None
    stream_id: str | None = None
    task_id: int | None = None
    message_content: str | None = None
    quoted_content: str | None = None
    file_info: dict[str, Any] | None = None
    response_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    response_at: datetime | None = None
    private: bool = False


def row_values(entry: ChatLogEntry) -> dict[str, Any]:
    """一行的列值：超长正文截断，工具去重。"""
    values = {f.name: getattr(entry, f.name) for f in dataclasses.fields(entry)}
    for key, limit in LIMITS.items():
        value = values[key]
        if isinstance(value, str) and len(value) > limit:
            values[key] = value[:limit]
    # 一轮里同一个工具会调很多次，统计要的是「用过哪些」；dict.fromkeys 去重且保序。
    values["tools_used"] = list(dict.fromkeys(entry.tools_used))
    return values


def elapsed_ms() -> ColumnElement[int]:
    """从 request_at 到事务时刻的毫秒数（封顶到 int 上限，陈年记录也不会溢出）。"""
    ms = func.extract("epoch", func.now() - ChatLog.request_at) * 1000
    return cast(func.least(ms, _MAX_LATENCY_MS), Integer)


async def _with_cost(session: AsyncSession, entry: ChatLogEntry, values: dict[str, Any]) -> None:
    if values["cost_usd"] is None:
        values["cost_usd"] = await estimate(
            session,
            model=entry.model,
            at=entry.request_at,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_read_tokens=entry.cache_read_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
        )


async def _reach(session: AsyncSession, entry: ChatLogEntry) -> None:
    """只有真实私聊才证明个人推送可达；群聊和 cron 都不能写入这个凭据。"""
    if entry.user_id is None or entry.chat_type != "single":
        return
    stmt = insert(UserReached).values(
        bot_id=entry.bot_id, user_id=entry.user_id, platform_chat_id=entry.chat_id or ""
    )
    await session.execute(
        stmt.on_conflict_do_nothing(index_elements=[UserReached.bot_id, UserReached.user_id])
    )


async def _insert_once(session: AsyncSession, entry: ChatLogEntry, values: dict[str, Any]) -> None:
    """补写一行终态；这个任务已经有记录就不写。

    同一任务的终态只有一个写入方：收尾与收尸靠任务行上的终态守卫互斥，开流前就结束的轮次
    只写这一次，所以先查后写不会写出两行。
    """
    if entry.task_id is not None:
        existing = await session.scalar(
            select(ChatLog.id).where(ChatLog.task_id == entry.task_id).limit(1)
        )
        if existing is not None:
            return
    session.add(ChatLog(**values))
    await _reach(session, entry)


async def _finish(session: AsyncSession, entry: ChatLogEntry) -> None:
    """改这个任务进行中的那一行；没有就补一行（开流前就结束，或开流时没写成）。"""
    values = row_values(entry)
    await _with_cost(session, entry, values)
    if entry.task_id is not None:
        updated = await session.scalar(
            update(ChatLog)
            .where(ChatLog.task_id == entry.task_id, ChatLog.status == CHAT_LOG_RUNNING)
            .values(**values)
            .returning(ChatLog.id)
        )
        if updated is not None:
            return
    await _insert_once(session, entry, values)


async def open_turn(session: AsyncSession, entry: ChatLogEntry) -> bool:
    """开流时在调用方事务里写进行中的一行；返回是否写成。

    与建会话、建流同一事务提交：聊天里出现会话查看链接时，这一行已经在库里。
    """
    values = row_values(entry)
    values.update(status=CHAT_LOG_RUNNING, latency_ms=None, response_at=None, cost_usd=None)
    await session.flush()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(ChatLog)
                .values(**values)
                .on_conflict_do_nothing(
                    # 谓词必须是字面量：写成绑定参数时，同一预备语句执行几次后 PostgreSQL
                    # 换用通用计划，就认不出这个部分唯一索引，插入直接报错。
                    index_elements=[ChatLog.task_id],
                    index_where=text(f"status = '{CHAT_LOG_RUNNING}'"),
                )
            )
            await _reach(session, entry)
    except Exception as exc:  # noqa: BLE001 写日志失败绝不能反过来弄挂对话
        log.warning("chat_log_open_failed", task_id=entry.task_id, error=type(exc).__name__)
        return False
    return True


async def finish_turn(session: AsyncSession, entry: ChatLogEntry) -> bool:
    """收尾时在调用方事务里写终态；返回是否写成，没写成的交给 `ChatLogWriter.submit_finish`。

    调用方必须在同一事务里刚抢到任务终态：记录与任务一起结束，不会出现任务结束了、记录还停在
    进行中。
    """
    await session.flush()
    try:
        async with session.begin_nested():
            await _finish(session, entry)
    except Exception as exc:  # noqa: BLE001 写日志失败绝不能反过来弄挂对话
        log.warning("chat_log_finish_failed", task_id=entry.task_id, error=type(exc).__name__)
        return False
    return True


async def insert_turn(session: AsyncSession, entry: ChatLogEntry) -> bool:
    """只补不改：这个任务还没有任何记录才写一行；返回是否写成（已有记录也算写成）。

    给 `close_running` 之后的兜底用：它返回 False 可能是写失败、那一行进行中的记录还在，
    这时不能拿一份残缺的记录去覆盖它，留给巡检按任务终态结掉。
    """
    values = row_values(entry)
    await session.flush()
    try:
        async with session.begin_nested():
            await _with_cost(session, entry, values)
            await _insert_once(session, entry, values)
    except Exception as exc:  # noqa: BLE001 写日志失败绝不能反过来弄挂收尾
        log.warning("chat_log_insert_failed", task_id=entry.task_id, error=type(exc).__name__)
        return False
    return True


async def close_running(
    session: AsyncSession,
    task_id: int,
    *,
    status: str,
    error_code: str | None,
    error_message: str | None,
) -> bool:
    """只知道结局、不知道这一轮内容时的收尾：保留开流时写下的内容，只改终态与耗时。

    返回是否真有一行进行中的记录被结掉；没有（或写失败）时由调用方决定要不要补一行，
    写失败留下的进行中记录由看护的巡检结掉。
    """
    limit = LIMITS["error_message"]
    await session.flush()
    try:
        async with session.begin_nested():
            closed = await session.scalar(
                update(ChatLog)
                .where(ChatLog.task_id == task_id, ChatLog.status == CHAT_LOG_RUNNING)
                .values(
                    status=status,
                    error_code=error_code,
                    error_message=error_message[:limit] if error_message else None,
                    latency_ms=elapsed_ms(),
                )
                .returning(ChatLog.id)
            )
    except Exception as exc:  # noqa: BLE001 写日志失败绝不能反过来弄挂收尾
        log.warning("chat_log_close_failed", task_id=task_id, error=type(exc).__name__)
        return False
    return closed is not None


class ChatLogWriter:
    """发射后不管的写入端，每条用自己的短事务，与对话事务解耦。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_pending: int = 500,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory = session_factory
        self._max_pending = max_pending
        self._sleep = sleep
        # 强引用：create_task 的返回值不留着，事件循环随时可能把它回收掉。
        self._tasks: set[asyncio.Task[bool]] = set()
        self.dropped = 0

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def write(self, entry: ChatLogEntry) -> bool:
        """补写一行终态（这个任务已有记录就跳过）。成功 True；重试耗尽或提交结果不明 False。"""
        values = row_values(entry)

        async def run(session: AsyncSession) -> None:
            await _with_cost(session, entry, values)
            await _insert_once(session, entry, values)

        return await self._attempt(run)

    async def finish(self, entry: ChatLogEntry) -> bool:
        """事后补写收尾：同事务没写成时用，改进行中的那一行或补一行。"""
        return await self._attempt(lambda session: _finish(session, entry))

    async def amend_response(self, task_id: int, response_content: str | None) -> bool:
        """收尾之后才定稿的回复（接了额度表的终稿）：只改已经结束的那一行的回复正文。"""
        limit = LIMITS["response_content"]
        text = response_content[:limit] if response_content else response_content

        async def run(session: AsyncSession) -> None:
            await session.execute(
                update(ChatLog)
                .where(ChatLog.task_id == task_id, ChatLog.status != CHAT_LOG_RUNNING)
                .values(response_content=text)
            )

        return await self._attempt(run)

    async def _attempt(self, run: Callable[[AsyncSession], Awaitable[None]]) -> bool:
        """在短事务里跑一次写入，失败按间隔重试；任何情况下都不抛。"""
        for attempt in range(len(_RETRY_DELAYS) + 1):
            committed = False
            try:
                async with self._factory() as session:
                    await run(session)
                    committed = True
                    await session.commit()
                return True
            except Exception as exc:  # noqa: BLE001 写日志失败绝不能反过来弄挂对话
                # 只记异常类型：values 里是用户消息与模型回复，一个字都不能进日志流。
                log.warning("chat_log_write_failed", attempt=attempt, error=type(exc).__name__)
                # committed 在 commit() 之前置位：commit 抛错时这条到底进没进库是二义的，
                # 重试就可能写出两条一模一样的日志，宁可丢。
                if committed or attempt >= len(_RETRY_DELAYS):
                    return False
                await self._sleep(_RETRY_DELAYS[attempt])
        return False

    def submit(self, entry: ChatLogEntry) -> None:
        """发射后不管：调用方不等、也不该等日志写完。"""
        self._spawn(self.write(entry))

    def submit_finish(self, entry: ChatLogEntry) -> None:
        self._spawn(self.finish(entry))

    def submit_amend(self, task_id: int, response_content: str | None) -> None:
        self._spawn(self.amend_response(task_id, response_content))

    def _spawn(self, work: Coroutine[Any, Any, bool]) -> None:
        if len(self._tasks) >= self._max_pending:
            work.close()
            self.dropped += 1
            if self.dropped % _DROP_LOG_EVERY == 1:
                log.error("chat_log_dropped", dropped=self.dropped, pending=len(self._tasks))
            return
        task = asyncio.create_task(work, name="chat-log-write")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float) -> None:  # noqa: ASYNC109
        """等在途的日志写完（进程优雅退出时调一次）。

        `timeout` 是这个接口的语义本身（「最多再等这么久，之后宁可丢也要退出」），不是可以
        外推给 `asyncio.timeout` 的调用超时，故豁免 ASYNC109。
        """
        if not self._tasks:
            return
        _, still = await asyncio.wait(set(self._tasks), timeout=timeout)
        if not still:
            return
        log.warning("chat_log_drain_timeout", cancelled=len(still))
        for task in still:
            task.cancel()
        # 再给取消一点时间落地就走人：这是退出路径，卡在这里比丢几条日志严重得多。
        await asyncio.wait(still, timeout=1.0)
