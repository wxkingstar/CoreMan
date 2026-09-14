"""把处理中的思考与正文按节流写入 task_streams（spec §6.3）。

每次写都是一个独立的短事务：对话本身在 relay 上跑几十分钟，绝不能让一条长事务
占着连接、也不能让写库失败的回滚牵连已经收到的增量。节流有两道闸——距上次写不足
`min_interval` 不写、内容一个字没变也不写——避免工具刷屏时把 task_streams 写爆。
"""

from __future__ import annotations

from typing import Any

from coreman.core.wecom.thinking import ThinkingCollector
from coreman.runtime.bus import streams
from coreman.runtime.worker.context import TaskContext


class StreamWriter:
    """一个任务一份的流状态缓冲。

    Attributes:
        thinking: 思考步骤收集器（🤔/💭/🔧/✨ 四类步骤）
        pending_text: 已累计的正文
        boundaries: 工具调用处的正文切分点，网关按它分段推送
        extra_lines: 排队 / 预警一类的提示行，恒排在思考区最前面
    """

    def __init__(self, ctx: TaskContext, *, min_interval: float = 0.5) -> None:
        self.ctx, self.min_interval = ctx, min_interval
        self.thinking = ThinkingCollector(clock=ctx.clock)
        self.pending_text = ""
        self.boundaries: list[int] = []
        self.extra_lines: list[str] = []
        self._last_written: tuple[str, str, tuple[int, ...]] | None = None
        self._last_flush = -1e9
        self.delivery: streams.Completion | None = None

    def add_text(self, delta: str) -> None:
        self.pending_text += delta

    def add_thinking(self, text: str) -> None:
        self.thinking.add_generating(text)

    def add_tool(self, name: str) -> None:
        self.thinking.add_tool_call(name)
        self.boundaries.append(len(self.pending_text))

    def set_thinking_line(self, text: str) -> None:
        self.extra_lines.append(text)

    def thinking_md(self) -> str:
        md = self.thinking.to_markdown()
        return "\n".join([*self.extra_lines, md]) if self.extra_lines else md

    def snapshot(self) -> tuple[str, str, list[int]]:
        return self.thinking_md(), self.pending_text, list(self.boundaries)

    async def flush(self, force: bool = False) -> bool:
        """写一次增量；被节流或内容未变时返回 False（不写库）。"""
        state = (self.thinking_md(), self.pending_text, tuple(self.boundaries))
        now = self.ctx.clock()
        if not force and (
            state == self._last_written or now - self._last_flush < self.min_interval
        ):
            return False
        async with self.ctx.session_factory() as session:
            delivery = await streams.update_state(
                session,
                self.ctx.task.id,
                thinking_md=state[0],
                pending_text=state[1],
                segment_boundaries=list(state[2]),
            )
            await session.commit()
        self.delivery = delivery
        self._last_written, self._last_flush = state, now
        return True

    async def complete(
        self, final_text: str, pending_card: dict[str, Any] | None = None
    ) -> streams.Completion:
        """收尾：把最后一版增量与终稿写在同一个事务里，网关只会看到一致的状态。

        返回收尾那一刻的投递状态（`delivery_mode` / `offset`）：网关的 drain / takeover 与
        这次收尾谁先谁后由行锁定夺，调用方据此决定终稿还要不要自己推。
        """
        async with self.ctx.session_factory() as session:
            await streams.update(
                session,
                self.ctx.task.id,
                thinking_md=self.thinking_md(),
                pending_text=self.pending_text,
                segment_boundaries=list(self.boundaries),
            )
            done = await streams.complete(
                session, self.ctx.task.id, final_text=final_text, pending_card=pending_card
            )
            await session.commit()
        return done
