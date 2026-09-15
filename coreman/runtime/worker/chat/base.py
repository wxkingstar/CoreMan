"""chat 流水线各阶段 mixin 的公共基类：节奏常量、实例属性与跨阶段调用的声明。

阶段按 spec §8.2 的顺序拆在同包的各个模块里，由 ChatTaskHandler 组合成一个处理器。
TYPE_CHECKING 里的方法只给类型检查器看跨模块调用的签名；运行时实现由组合进来的阶段
mixin 提供，子类（如提交轮）照旧逐个覆盖。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import streams
from coreman.runtime.worker.background import Timing
from coreman.runtime.worker.chat.models import Intake, Outcome, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext


class ChatStageBase:
    SILENT_WARN_SECONDS = 30.0
    TICK_SECONDS = 1.0
    LONG_TASK_SECONDS = 60
    # 长任务完成提醒延后入队的秒数：让网关先推终稿的 finish 帧，提醒不跑到回复前面。
    LONG_TASK_NOTICE_DELAY_SECONDS = 3.0
    QUEUED_NOTICE_SECONDS = 5.0
    SUPERSEDE_POLL_SECONDS = 0.1
    SUPERSEDE_POLLS = 200

    timing: Timing
    _on_first_event: Callable[[], None] | None

    if TYPE_CHECKING:

        async def _sessions(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
            raise NotImplementedError

        def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
            raise NotImplementedError

        async def _finalize(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> None:
            raise NotImplementedError

        @staticmethod
        async def _icon_url(ctx: TaskContext) -> str:
            raise NotImplementedError

        async def _push_if_proactive(
            self,
            ctx: TaskContext,
            pre: Prepared,
            verdict: Verdict,
            done: streams.Completion,
            card: dict[str, Any] | None = None,
            *,
            plain: bool = False,
        ) -> None:
            raise NotImplementedError
