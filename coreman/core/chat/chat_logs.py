"""对话日志落库。

写日志永远不能拖慢、更不能弄挂一轮对话：`submit` 是发射后不管，`write` 自己吞掉所有异常，
积压到上限就丢弃并计数。日志内容（用户消息、模型回复）只进库、绝不进日志流。
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import ChatLog, UserReached
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


@dataclass
class ChatLogEntry:
    """chat_logs 的一行（除 id / created_at 外的全部可写列）。

    可变 dataclass：工作进程在请求开始时建好前半段，流跑完再补 response / token / 耗时。
    """

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


class ChatLogWriter:
    """chat_logs 的写入端。每条日志用自己的短事务，与对话事务完全解耦。"""

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

    def _values(self, entry: ChatLogEntry) -> dict[str, Any]:
        values = {f.name: getattr(entry, f.name) for f in dataclasses.fields(entry)}
        for key, limit in LIMITS.items():
            value = values[key]
            if isinstance(value, str) and len(value) > limit:
                values[key] = value[:limit]
        # 一轮里同一个工具会调很多次，统计要的是「用过哪些」；dict.fromkeys 去重且保序。
        values["tools_used"] = list(dict.fromkeys(entry.tools_used))
        return values

    async def write(self, entry: ChatLogEntry) -> bool:
        """同步写一条。成功 True；重试耗尽或提交结果不明 False——任何情况下都不抛。"""
        values = self._values(entry)
        for attempt in range(len(_RETRY_DELAYS) + 1):
            committed = False
            try:
                async with self._factory() as session:
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
                    session.add(ChatLog(**values))
                    # 只有真实私聊才证明个人推送可达；群聊和 cron 都不能写入这个凭据。
                    if entry.user_id is not None and entry.chat_type == "single":
                        stmt = insert(UserReached).values(
                            bot_id=entry.bot_id,
                            user_id=entry.user_id,
                            platform_chat_id=entry.chat_id or "",
                        )
                        await session.execute(
                            stmt.on_conflict_do_nothing(
                                index_elements=[UserReached.bot_id, UserReached.user_id]
                            )
                        )
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
        if len(self._tasks) >= self._max_pending:
            self.dropped += 1
            if self.dropped % _DROP_LOG_EVERY == 1:
                log.error("chat_log_dropped", dropped=self.dropped, pending=len(self._tasks))
            return
        task = asyncio.create_task(self.write(entry), name="chat-log-write")
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
