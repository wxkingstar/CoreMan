"""chat 流水线各阶段之间传递的数据：入站解析、开流上下文、消费结果与收尾判定。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from coreman.core.chat import sessions
from coreman.core.chat.content import BuiltContent
from coreman.core.db.models import (
    Bot,
    InboundEvent,
    RelayServer,
)
from coreman.core.prompting import Speaker
from coreman.core.relay.client import ChatRequest
from coreman.core.relay.sse import UsageEvent
from coreman.runtime.worker.background import TimeoutSupervisor
from coreman.runtime.worker.stream_writer import StreamWriter


@dataclass(frozen=True)
class Intake:
    """入站解析的结果：够判断要不要继续，也够拼一条 chat_logs。

    `relay` 可空：机器人没绑 relay 也要留下一条 error 审计，这一步之后的流程才保证非空
    （见 `Prepared.relay`）。
    """

    bot: Bot
    relay: RelayServer | None
    inbound: InboundEvent
    speaker: Speaker
    chat_id: str
    chat_type: str
    session_key: str
    text: str
    message_type: str

    @property
    def relay_id(self) -> uuid.UUID | None:
        return self.relay.id if self.relay is not None else None


@dataclass(frozen=True)
class Prepared:
    """开流之后的全部上下文；`_converse` / `_finalize` 只读它。"""

    intake: Intake
    relay: RelayServer
    info: sessions.SessionInfo
    request: ChatRequest
    writer: StreamWriter
    session_url: str
    started_clock: float
    supervisor: TimeoutSupervisor
    # 开流之后才组装（下载要写提示行），所以这里可空：`_prepare` 交出去之前一定已填上。
    content: BuiltContent | None = None


@dataclass
class Outcome:
    """一轮 SSE 消费的结果。分类只看这里，不再回头翻流。

    计数自己数：`chat_stream` 不把解析器的 counts 暴露出来，而「零事件」判定要的正是
    text/thinking/tool/usage 四类的总数（`FinishEvent` 只是信息，不算事件）。
    """

    request_started: float | None = None
    text_events: int = 0
    thinking_events: int = 0
    tool_events: int = 0
    usage_events: int = 0
    tools: list[str] = field(default_factory=list)
    usage: UsageEvent | None = None
    saw_event: bool = False
    finish_reason: str | None = None
    relay_error: bool = False
    ask_user: bool = False
    questions: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    reason: str | None = None
    error: Exception | None = None

    @property
    def counted(self) -> int:
        return self.text_events + self.thinking_events + self.tool_events + self.usage_events


@dataclass(frozen=True)
class Verdict:
    """收尾判定：给 chat_logs 的状态、给 tasks 的状态、给用户的终稿。"""

    log_status: str
    task_status: str
    error_code: str | None
    error_message: str | None
    final_text: str
