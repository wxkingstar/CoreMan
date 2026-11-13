"""个人助理子任务的一轮：在子任务自己的会话里跑，不往飞书发任何东西，收尾时把终稿记为结论。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.bus import streams, tasks
from coreman.core.chat import sessions
from coreman.core.chat.rate_limit import is_rate_limited
from coreman.core.db.models import Bot
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.chat.models import Intake, Outcome, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler


class AssistantSubtaskHandler(PlatformTurnHandler):
    """文本取自子任务这一行（首轮是交代，之后是追问）；结论写回这一行，不发到所有者的私聊。

    `_text` 只读：基类在占会话之前调它，让路放回队列后每次重新认领都会再调一次。子任务会话
    只有它自己的轮次，所有者的消息在主会话键上，两边互不取消。没跑到收尾的结局（开流前被
    取消、暂停、被收尸）由 scheduler 的 `jobs.reconcile` 收口。

    不出声的几处都在这里：流的回复上下文带 `_silent`（网关不推）、看护器不推进度与超时通知、
    不发长任务完成提醒、不自己推终稿、不开选择卡、触限时不发切换卡、会话忙时不回提示。
    """

    kind = SUBTASK_KIND
    # 子任务会话不是主会话，本来就不参与每日换新；写明免得以后改了判断条件把它算进去。
    DAILY_SESSIONS = False
    INACTIVE = "subtask_inactive"
    OWNER_CHANGED = "subtask_owner_changed"
    ANNOUNCED = "subtask_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        row = await jobs.for_round(session, bot.id, ctx.task)
        if row is None:
            return None
        ctx.log.info("assistant_subtask_opened", job_id=str(row.id), round=row.rounds)
        return jobs.round_text(row)

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        info = await super()._session_info(session, ctx, intake, backend)
        # 与开流同一事务：这一轮真的开了，子任务才算在跑。
        await jobs.mark_running(session, intake.bot.id, ctx.task)
        return info

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        return {"reply_context": {**intake.inbound.reply_context, "_silent": True}}

    def _after_supervisor(self, pre: Prepared) -> None:
        pre.supervisor.silent = True

    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        verdict = super()._classify(ctx, pre, out)
        if verdict.log_status == "ask_user":
            # 没有人能答题：不开选择卡（它会挂在所有者的私聊上），题面留在结论里交回主会话。
            return replace(verdict, log_status="success")
        if verdict.log_status in ("success", "error") and is_rate_limited(verdict.final_text):
            # 终稿是「额度用完」的告示，不是结论：按失败收尾，由结果回合告诉主会话。
            return Verdict("error", "failed", "rate_limited", "运行时额度用完", verdict.final_text)
        return verdict

    async def _postprocess_rate_limit(
        self, ctx: TaskContext, pre: Prepared, verdict: Verdict
    ) -> tuple[Verdict, dict[str, Any] | None]:
        """不附额度表、不发切换卡：那张卡会发到所有者的私聊，并覆盖他在那里的待答状态。"""
        return verdict, None

    async def _notify_long_task(
        self,
        ctx: TaskContext,
        pre: Prepared,
        verdict: Verdict,
        done: streams.Completion,
        elapsed: int,
    ) -> None:
        """子任务跑多久都不往私聊里发完成提醒：结论由结果回合带回去。"""

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
        """流被网关改成主动模式时也不自己推终稿。"""

    async def _session_busy(self, ctx: TaskContext, intake: Intake) -> None:
        """子任务会话里上一轮停不下来：不回提示，只把这一轮记失败（对账会把子任务收口）。"""
        async with ctx.session_factory() as session:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="session_busy", only_active=True
            )
            await session.commit()
        ctx.log.warning("assistant_subtask_session_busy", attempts=ctx.task.attempts)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        await super()._after_finalize(ctx, pre, verdict)
        job_id = jobs.job_id_of(ctx.task.session_key)
        if job_id is None:
            return
        ok = verdict.task_status == "succeeded"
        body = verdict.final_text.removesuffix(msg("done_suffix", ctx.locale)).strip()
        async with ctx.session_factory() as session:
            bot = await session.get(Bot, ctx.task.bot_id)
            if bot is None:
                return
            row = await jobs.complete(
                session,
                bot,
                job_id,
                task_id=ctx.task.id,
                ok=ok,
                result=ctx.redact(body) if ok else None,
                error_code=verdict.error_code,
            )
            await session.commit()
        ctx.log.info(
            "assistant_subtask_finished",
            job_id=str(job_id),
            status=row.status if row is not None else "stale",
        )
