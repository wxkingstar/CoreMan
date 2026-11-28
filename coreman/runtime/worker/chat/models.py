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
    # 飞书群里真人只有发话人自己（`chat.solo`，每轮实时判断）：安全策略按私聊走，投递仍回这个群。
    solo: bool = False

    @property
    def private(self) -> bool:
        """这一轮按私聊的安全策略处理：真正的私聊，或单人群。"""
        return self.chat_type == "single" or self.solo

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
    # 给人看的实例名（节点名 / provider），出错提示里用它，不用内部名。
    relay_label: str = ""
    # 写进后台阶段进程的一轮：开工那条消息问的是什么（开头一句），收尾时用它指明后台结果回到哪里。
    live_origin: str = ""


@dataclass
class Outcome:
    """一轮 SSE 消费的结果。分类只看这里，不再回头翻流。

    计数自己数：`chat_stream` 不把解析器的 counts 暴露出来，而「零事件」判定要的正是
    text/thinking/tool/usage 四类的总数（`FinishEvent` 只是信息，不算事件）。
    """

    collaboration_handoff: bool = False
    request_started: float | None = None
    text_events: int = 0
    thinking_events: int = 0
    tool_events: int = 0
    usage_events: int = 0
    tools: list[str] = field(default_factory=list)
    usage: UsageEvent | None = None
    # 运行时随响应带回的订阅额度原样保存，收尾时校验后写入实例。
    rate_limits: dict[str, Any] | None = None
    runtime_seat_id: str | None = None
    actual_model: str | None = None
    saw_event: bool = False
    finish_reason: str | None = None
    relay_error: bool = False
    ask_user: bool = False
    questions: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    reason: str | None = None
    error: Exception | None = None
    # 主动插话跑得久，已经在群里先回过「我看一下」。
    observe_acked: bool = False
    # 后台阶段：主回合结束时还有后台任务（`x_runtime.background`），带最后一份快照与到期时间。
    background: bool = False
    background_tasks: list[dict[str, Any]] = field(default_factory=list)
    background_deadline: int | None = None
    # 这条消息写进了后台阶段里的进程（`x_runtime.live_joined`），带上那一刻的后台任务。
    live_joined: bool = False
    live_join_tasks: list[dict[str, Any]] = field(default_factory=list)
    # 驱动拒收（409）：`live_owner_mismatch` / `not_live`，还没开始执行，可以调整后重发。
    live_conflict: str | None = None

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
