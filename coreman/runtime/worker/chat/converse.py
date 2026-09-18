"""消费阶段：逐帧消费 SSE、驱动两阶段超时，结果只记进 Outcome。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from coreman.core.db.models import Task
from coreman.core.relay.sse import (
    AskUserQuestionEvent,
    FinishEvent,
    RelayErrorEvent,
    SseEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
    UsageEvent,
)
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Outcome, Prepared
from coreman.runtime.worker.context import TaskContext


async def next_event(gen: AsyncIterator[SseEvent]) -> SseEvent | None:
    """取下一个事件，流结束返回 None。

    包一层是为了让它成为一个普通协程：调用方要把它塞进 `asyncio.Task` 才能跨越「静默
    超时」继续活着，而 `StopAsyncIteration` 穿过 Task 边界的行为并不友好。
    """
    try:
        return await anext(gen)
    except StopAsyncIteration:
        return None


class ConverseStage(ChatStageBase):
    """消费 SSE。"""

    async def _converse(self, ctx: TaskContext, pre: Prepared) -> Outcome:
        out = Outcome(request_started=ctx.clock())
        client = ctx.relay_client_factory(pre.relay)
        # 显式关闭生成器才能断开连接，让 relay 停止当前 CLI。
        gen = client.chat_stream(
            pre.request, total_timeout=float(pre.intake.bot.sse_timeout_seconds)
        )
        supervisor = pre.supervisor
        stopping = asyncio.ensure_future(ctx.cancel_event.wait())
        pending: asyncio.Task[SseEvent | None] | None = None
        silent_since = ctx.clock()
        guard_checked = silent_since - 1
        # 飞书个人工具叠加在普通助手上，不受协作轮次的工具预算与逐秒复核约束。
        guarded = bool(
            pre.request.env_vars.get("COREMAN_COLLABORATION_TOKEN")
            or ctx.task.payload.get("collaboration_id")
        )
        try:
            while True:
                if ctx.cancel_event.is_set():
                    out.cancelled, out.reason = True, ctx.cancel_reason
                    return out
                if pending is None:
                    pending = asyncio.ensure_future(next_event(gen))
                # 1 秒一跳：超时判定不能依赖「下一帧什么时候来」，沉默的流也要按点切后台。
                done, _ = await asyncio.wait(
                    {pending, stopping},
                    timeout=self.TICK_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                now = ctx.clock()
                if guarded and now - guard_checked >= 1:
                    guard_checked = now
                    async with ctx.session_factory() as session:
                        task = await session.get(Task, ctx.task.id)
                        if task is None or task.cancel_requested_at:
                            out.cancelled, out.reason = (
                                True,
                                task.cancel_reason if task else "reaper",
                            )
                            return out
                        if task.payload.get("collaboration_handoff"):
                            out.collaboration_handoff = True
                            return out
                if await supervisor.tick(now) == "expired":
                    # 硬 TTL：返回即可，finally 会断连（=让 relay 杀掉这一轮 CLI）。
                    out.cancelled, out.reason = True, "hard_ttl"
                    return out
                if pending in done:
                    event, pending = pending.result(), None
                    if event is None:
                        return out
                    silent_since = now
                    self._apply(ctx, pre, out, event)
                    await pre.writer.flush()
                    if guarded and out.tool_events >= 64:
                        out.cancelled, out.reason = True, "collaboration_budget_exhausted"
                        return out
                elif not done and now - silent_since >= self.SILENT_WARN_SECONDS:
                    # 静默只提醒、不中断：工具跑十几分钟不吐字是正常的，中断才是事故。
                    ctx.log.warning("sse_silent", seconds=int(now - silent_since))
                    silent_since = now
                await supervisor.on_progress(now, pre.writer.pending_text, pre.writer.boundaries)
                if out.ask_user:
                    return out
        except Exception as exc:  # noqa: BLE001 任何异常都只算这一轮失败，统一进 _classify
            ctx.log.warning("task_failed", error=type(exc).__name__)
            out.error = exc
            return out
        finally:
            stopping.cancel()
            if pending is not None:
                # 先把还在等下一帧的那个 Task 停掉，生成器才关得动；关掉 = 断开 = 让 relay 停任务。
                pending.cancel()
                await asyncio.wait({pending})
            await gen.aclose()
            await client.aclose()

    def _apply(self, ctx: TaskContext, pre: Prepared, out: Outcome, event: SseEvent) -> None:
        if not out.saw_event:
            # 单独记一个「见过帧」的标志：`counted` 不数 FinishEvent，用它判首帧会重复触发。
            out.saw_event = True
            from coreman.core.observability.metrics import FIRST_BYTE

            if out.request_started is not None:
                FIRST_BYTE.labels(str(pre.relay.id)).observe(
                    max(0, ctx.clock() - out.request_started)
                )
            ctx.log.info("first_event", event_type=type(event).__name__)
            if self._on_first_event is not None:
                self._on_first_event()
        writer = pre.writer
        if isinstance(event, TextDelta):
            out.text_events += 1
            writer.add_text(event.text)
        elif isinstance(event, ThinkingDelta):
            out.thinking_events += 1
            writer.add_thinking(event.text)
        elif isinstance(event, ToolUseStart):
            out.tool_events += 1
            out.tools.append(event.name)
            writer.add_tool(event.name)
        elif isinstance(event, RelayErrorEvent):
            out.relay_error = True
            writer.add_text(event.text)
        elif isinstance(event, UsageEvent):
            out.usage_events += 1
            out.usage = event
        elif isinstance(event, AskUserQuestionEvent):
            out.ask_user, out.questions = True, list(event.questions)
        elif isinstance(event, FinishEvent):
            out.finish_reason = event.reason
