"""任务处理上下文：一个任务一份，跨处理器共享取消信号与依赖。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.auth.external_key import ExternalKey
from coreman.core.bus import tasks as bus_tasks
from coreman.core.chat import redaction
from coreman.core.chat.chat_logs import ChatLogWriter
from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer, Task
from coreman.core.logging import get_logger
from coreman.core.relay.client import RelayClient
from coreman.core.settings_store import SettingsStore

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
    # 管理台对外地址：会话查看链接指向管理台对运行时节点的反向代理。
    public_base_url: str = ""
    # 部署配置的外部签发方密钥（BOT_JWT_*）；有则业务系统令牌用它签。
    external_jwt_key: ExternalKey | None = None
    # 两个共享依赖由 WorkerService 建一份传下来：解析器的缓存、媒体客户端的连接池都靠这份复用。
    openuserid: OpenUseridResolver | None = None
    media_fetcher: MediaFetcher | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str | None = None
    stream_id: str | None = None
    # 本轮注入 CLI 的凭据；开流阶段按最终 env 填好，出站文本与对话记录都按它过滤。
    secrets: frozenset[str] = frozenset()
    # 这一轮挂了本人的企业微信工具：对话记录只给本人看。
    private_turn: bool = False
    # 这一轮可以向其他 AI 员工求助：执行轮次的工具数封顶，防 A→B→A 式的循环消耗。
    # 只挂了同事时不封顶，人工答复不会形成循环。
    bot_peers_mounted: bool = False
    # init=False：由 __post_init__ 绑好任务字段再交出去，调用方不该也不能自己传。
    log: structlog.stdlib.BoundLogger = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.log = get_logger("coreman.runtime.worker.task").bind(
            task_id=self.task.id, bot_id=str(self.task.bot_id)
        )

    def redact(self, text: str | None) -> str | None:
        """出站文本的统一过滤口：凡是用户看得到、或要进 chat_logs 的正文都过这里。"""
        return redaction.redact(text, self.secrets)

    def redact_json(self, value: Any) -> Any:
        """递归过滤结构里的字符串：会落库、之后再渲染给用户的模型产物走这里。"""
        return redaction.redact_value(value, self.secrets)

    def redact_text(self, text: str) -> redaction.Redacted:
        """带下标换算的版本，给同时要挪 `segment_boundaries` 的调用方。"""
        return redaction.redact_text(text, self.secrets)

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
