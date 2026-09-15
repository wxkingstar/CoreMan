"""chat 任务处理器（spec §8.2 流水线、§8.3 请求构造、§8.4 事件与分类）。

流水线顺序即优先级：机器人停用 → 公告 → 白名单 → 内置命令 → 待答文本答案 / 取消 →
relay 不可用 → 同会话串行 → 会话/提示词/env → 开流 → 组装内容（下载媒体）→ 消费 SSE →
分类收尾。每一步要么放行到下一步，要么就地给用户一个回复并把任务结成终态，绝不留下
「流开着没人收尾」的状态。

组装内容排在开流之后：下载几十兆的附件要十几秒，「正在下载…」得有个思考区写进去，
用户才知道不是卡死了；媒体拿不到就在这里把这一轮结掉，不拿半条消息去问模型。

relay 检查排在最后：公告、拒绝、命令这几条路都不碰 relay，机器人没绑可用实例时它们照样
该答什么答什么，不该被一句「relay 未配置」顶掉。

处理器里只有主流程一条协程：任务心跳（取消的兜底传导与失联判定）由 WorkerService 的
心跳循环统一写，这里不再另起一条。取消（用户 stop / 被新消息替代 / 超时）统一走
`ctx.cancel_event`，消费循环每一轮开头都看一眼；断开 relay 连接就等于让 relay
杀掉那一轮 CLI，所以取消时是 `aclose()` 生成器而不是干等它跑完。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from coreman.core.bus import tasks
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.background import DEFAULT_TIMING, Timing
from coreman.runtime.worker.chat.classify import ClassifyStage
from coreman.runtime.worker.chat.content import ContentStage
from coreman.runtime.worker.chat.converse import ConverseStage
from coreman.runtime.worker.chat.finalize import FinalizeStage
from coreman.runtime.worker.chat.intake import IntakeStage
from coreman.runtime.worker.chat.models import Intake, Prepared
from coreman.runtime.worker.chat.opening import OpenStage
from coreman.runtime.worker.chat.session_switch import SessionSwitchStage
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import reply_once


class ChatTaskHandler(
    IntakeStage,
    SessionSwitchStage,
    OpenStage,
    ContentStage,
    ConverseStage,
    ClassifyStage,
    FinalizeStage,
):
    """spec §8.2 的主流水线。各阶段实现在同包模块里，这里组合它们并串起前置流程。"""

    kind = "chat"

    def __init__(self, *, timing: Timing = DEFAULT_TIMING) -> None:
        self.timing = timing
        self._on_first_event: Callable[[], None] | None = None  # 测试钩子

    async def run(self, ctx: TaskContext) -> None:
        pre = await self._prepare(ctx)
        if pre is None:
            return
        outcome = await self._converse(ctx, pre)
        await self._finalize(ctx, pre, outcome)

    async def _prepare(self, ctx: TaskContext) -> Prepared | None:
        started = ctx.clock()
        async with ctx.session_factory() as session:
            resolved = await self._resolve(session, ctx)
            await session.commit()
        if resolved is None:
            return None
        intake, relay, parts = resolved
        if ctx.task.attempts > 1 and await self._outdated(ctx, intake):
            return None
        # supersede 必须先提交再等：被替代的任务跑在另一条连接上，看不见未提交的取消标记。
        async with ctx.session_factory() as session:
            await tasks.supersede(
                session, intake.bot.id, intake.session_key, except_task_id=ctx.task.id
            )
            await session.commit()
        try:
            ready = await self._wait_superseded(ctx, intake.bot.id, intake.session_key)
        except TimeoutError:
            await self._session_busy(ctx, intake)
            return None
        if not ready:
            await self._cancelled_before_open(ctx)
            return None
        async with ctx.session_factory() as session:
            pre = await self._open(session, ctx, intake, relay, started)
            await session.commit()
        # 首帧必须等流建好并提交之后再写：flush 走的是另一个事务，看不见未提交的那一行。
        await pre.writer.flush(force=True)
        if self._needs_content():
            built = await self._build_content(ctx, pre, parts)
            if built is None:
                return None
            pre = replace(
                pre,
                content=built,
                request=replace(pre.request, user_content=self._user_content(built)),
            )
        ctx.log.info("task_started", bot_key=intake.bot.bot_key, chat_type=intake.chat_type)
        return pre

    async def _session_busy(self, ctx: TaskContext, intake: Intake) -> None:
        """上一轮迟迟停不下来：新消息不丢，放回队列稍后再认领；重排到头才告诉用户。

        等待期间还没开流、没碰会话，放回队列是干净的；再认领时照旧先等更早的任务退出，
        同会话串行不变。
        """
        if ctx.task.attempts < self.SUPERSEDE_MAX_ATTEMPTS:
            async with ctx.session_factory() as session:
                deferred = await tasks.defer(
                    session, ctx.task.id, seconds=self.SUPERSEDE_RETRY_SECONDS
                )
                await session.commit()
            if deferred:
                ctx.log.warning("session_busy_deferred", attempts=ctx.task.attempts)
                return
            # 没放回去：本任务刚被请求取消（stop / 更新的消息），按取消收尾。
            await self._cancelled_before_open(ctx)
            return
        ctx.log.warning("session_busy_gave_up", attempts=ctx.task.attempts)
        async with ctx.session_factory() as session:
            await reply_once(
                session,
                ctx,
                reply_context=intake.inbound.reply_context,
                text=msg("session_busy", ctx.locale),
            )
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="session_busy", only_active=True
            )
            await session.commit()

    async def _outdated(self, ctx: TaskContext, intake: Intake) -> bool:
        """重排回来时同会话已有更新的消息：这一条已被它替代，不再作答（与运行中被替代同理）。"""
        async with ctx.session_factory() as session:
            newer = await tasks.has_newer(
                session,
                intake.bot.id,
                intake.session_key,
                task_id=ctx.task.id,
                kind=ctx.task.kind,
            )
            if newer:
                await tasks.finish(
                    session,
                    ctx.task.id,
                    status="cancelled",
                    error_code="superseded",
                    only_active=True,
                )
            await session.commit()
        if newer:
            ctx.log.info("deferred_task_superseded")
        return newer

    async def _cancelled_before_open(self, ctx: TaskContext) -> None:
        """还没开流就被取消：没有气泡要收尾，stop 的回执由命令自己发，只写终态。"""
        async with ctx.session_factory() as session:
            await tasks.finish(
                session,
                ctx.task.id,
                status="cancelled",
                error_code=ctx.cancel_reason or "cancelled",
                only_active=True,
            )
            await session.commit()
        ctx.log.info("cancelled_before_open", reason=ctx.cancel_reason)
