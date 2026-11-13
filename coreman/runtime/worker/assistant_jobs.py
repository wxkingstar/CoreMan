"""个人助理子任务的两种轮次：子任务自己的一轮（不往飞书发任何东西，终稿记为结论），
以及把结论投回主会话的一轮。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import compiling, jobs
from coreman.core.assistants.kinds import JOB_RESULT_KIND, SUBTASK_KIND
from coreman.core.chat import sessions
from coreman.core.db.models import Bot
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.chat.models import Intake, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler, SilentTurnHandler


class AssistantSubtaskHandler(SilentTurnHandler):
    """文本取自子任务这一行（首轮是交代，之后是追问）；结论写回这一行，不发到所有者的私聊。

    `_text` 只读：基类在占会话之前调它，让路放回队列后每次重新认领都会再调一次。子任务会话
    只有它自己的轮次，所有者的消息在主会话键上，两边互不取消。没跑到收尾的结局（开流前被
    取消、暂停、被收尸）由 scheduler 的 `jobs.reconcile` 收口。

    不出声的几处在基类 `SilentTurnHandler` 里。
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
            if row is not None:
                # 平台派的下班编译做完了：推进它的活动窗口。
                await compiling.settle(session, row)
            await session.commit()
        ctx.log.info(
            "assistant_subtask_finished",
            job_id=str(job_id),
            status=row.status if row is not None else "stale",
        )


class AssistantJobResultHandler(PlatformTurnHandler):
    """把子任务的结论作为一轮投回主会话：文本由平台按行生成，结论在引用行里。

    `_text` 只读。主会话里的一轮会给所有者的消息让路、会被它取消：走到收尾的在这里记账，
    没走到的由 scheduler 的 `jobs.reconcile` 放回重投。
    """

    kind = JOB_RESULT_KIND
    INACTIVE = "job_result_inactive"
    OWNER_CHANGED = "job_result_owner_changed"
    ANNOUNCED = "job_result_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        row = await jobs.bound_report(session, ctx.task)
        if row is None:
            return None
        ctx.log.info("assistant_job_result_opened", job_id=str(row.id), status=row.status)
        return jobs.result_text(row)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        await super()._after_finalize(ctx, pre, verdict)
        async with ctx.session_factory() as session:
            settled = await jobs.settle_report(
                session, ctx.task.id, verdict.task_status, verdict.error_code
            )
            await session.commit()
        ctx.log.info("assistant_job_result_settled", status=verdict.task_status, settled=settled)
