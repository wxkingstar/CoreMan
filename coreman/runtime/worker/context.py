"""任务处理上下文：一个任务一份，跨处理器共享取消信号与依赖。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.chat.chat_logs import ChatLogWriter
from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer, Task
from coreman.core.logging import get_logger
from coreman.core.relay.client import RelayClient
from coreman.core.settings_store import SettingsStore
from coreman.runtime.bus import tasks as bus_tasks

if TYPE_CHECKING:  # 只用于类型标注，避免 worker 启动时连带拉起企微与解析器模块。
    from coreman.core.chat.openuserid import OpenUseridResolver
    from coreman.core.wecom.media import MediaFetcher


@dataclass
class TaskContext:
    task: Task
    instance_id: str
    session_factory: async_sessionmaker[AsyncSession]
    settings_store: SettingsStore
    cipher: Cipher
    relay_client_factory: Callable[[RelayServer], RelayClient]
    chat_logs: ChatLogWriter
    clock: Callable[[], float] = time.monotonic
    locale: str = "zh"
    # 两个共享依赖由 WorkerService 建一份传下来：解析器的缓存、媒体客户端的连接池都靠这份复用。
    openuserid: OpenUseridResolver | None = None
    media_fetcher: MediaFetcher | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str | None = None
    stream_id: str | None = None
    # init=False：由 __post_init__ 绑好任务字段再交出去，调用方不该也不能自己传。
    log: structlog.stdlib.BoundLogger = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.log = get_logger("coreman.runtime.worker.task").bind(
            task_id=self.task.id, bot_id=str(self.task.bot_id)
        )

    def request_cancel(self, reason: str) -> None:
        if not self.cancel_event.is_set():
            self.cancel_reason = reason
            self.cancel_event.set()

    async def heartbeat(self) -> None:
        async with self.session_factory() as session:
            state, reason = await bus_tasks.heartbeat(session, self.task.id)
            await session.commit()
        if state is bus_tasks.Heartbeat.CANCELLED:
            self.request_cancel(reason or "cancelled")
        elif state is bus_tasks.Heartbeat.LOST:
            # 行已不在 ACTIVE：库抖动让两路心跳都超了 60 秒，reaper 已经判我失联、替我写了
            # 终态并通知了用户。继续跑就是白烧 relay，收尾还会把任务改回 succeeded、把终稿
            # 改写成永远送不出去的文本，所以就地走取消路径把这一轮断掉。
            self.log.warning("task_row_lost", task_id=self.task.id)
            self.request_cancel("reaper")

    def bind_stream(self, stream_id: str) -> None:
        self.stream_id = stream_id
        structlog.contextvars.bind_contextvars(stream_id=stream_id, task_id=self.task.id)
