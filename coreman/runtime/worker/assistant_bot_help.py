"""个人助理调度来的一轮：在目标员工自己的独立会话里、以所有者的身份跑，不往飞书发任何东西。

流水线（会话、提示词、业务系统令牌、收尾）与所有者私聊这位员工的一轮相同，不同的只有：
- 入站解析：没有入站事件，用一个不落库的替身；问题从助理的 `assistant_jobs` 密文行里取；
  开轮前重新核对资格（`dispatch.authorize`）。
- 不出声：继承 `SilentTurnHandler`——流的回复上下文带 `_silent`（网关不推、流恢复不补发）、
  看护器不推进度与超时通知、不发长任务完成提醒、不自己推终稿、不发额度切换卡；任务类型登记
  在 `kinds.SILENT_KINDS`，收尸不往会话里发「进程没了」。会话忙时不回提示，记失败并投回。
- 终稿由平台解析（纯 JSON），不给人看。
基类 `PlatformTurnHandler` 的入站解析只认助理自己的轮次，这里整个换掉：这一轮跑在普通员工
名下。没走到收尾的结局（被取消、worker 失联）由 scheduler 的 `jobs.reconcile` 收口。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import dispatch
from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.bus import tasks
from coreman.core.chat import sessions
from coreman.core.chat.announcements import find_announcement
from coreman.core.db.models import InboundEvent, RelayServer, Task
from coreman.core.i18n.messages import msg
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat.models import Intake, Outcome, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import SilentTurnHandler


def stand_in(bot_id: uuid.UUID, task: Task, platform_user_id: str) -> InboundEvent:
    """这一轮的入站替身：不加入会话、不落库，只给流水线提供会话坐标。

    会话号用求助自己的会话键：不是所有者与这位员工的私聊，也不是任何一个群，往它发不出消息。
    """
    key = task.session_key or ""
    return InboundEvent(
        bot_id=bot_id,
        platform="feishu",
        platform_msg_id=f"assistant-help:{task.id}",
        kind="message",
        chat_type="single",
        chat_id=key,
        sender_platform_user_id=platform_user_id,
        sender_open_id="",
        payload={},
        reply_context={"chat_id": key, "chat_type": "single"},
        received_at=utcnow(),
    )


class AssistantBotHelpHandler(SilentTurnHandler):
    """入站解析换成求助自己的；不出声的几处在基类里，这里只补上「结局交回助理」。"""

    kind = BOT_HELP_KIND
    # 取消任务时写的错误代码：这一轮已经没有意义、所有者不再能调度它、运行时有公告。
    INACTIVE = "bot_help_inactive"
    NOT_ALLOWED = "bot_help_not_allowed"
    ANNOUNCED = "bot_help_announced"

    async def _refuse(
        self, session: AsyncSession, ctx: TaskContext, status: str, code: str
    ) -> None:
        """这一轮不开：结掉任务，并把求助记失败、投回助理主会话。"""
        await tasks.finish(session, ctx.task.id, status=status, error_code=code)
        await dispatch.fail(session, ctx.task, code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        found = await dispatch.for_round(session, ctx.task)
        if found is None:
            await tasks.finish(session, ctx.task.id, status="cancelled", error_code=self.INACTIVE)
            return None
        try:
            # 派出之后员工被停用、关了接受求助、可用人名单变了，或所有者的账号、身份变了。
            target, speaker = await dispatch.authorize(session, found.assistant, ctx.task.bot_id)
        except dispatch.DispatchError:
            await self._refuse(session, ctx, "cancelled", self.NOT_ALLOWED)
            return None
        relay = (
            await session.get(RelayServer, target.relay_server_id)
            if target.relay_server_id
            else None
        )
        if relay is None or not relay.is_active:
            await self._refuse(session, ctx, "failed", "relay_unavailable")
            return None
        hit = await find_announcement(
            session, bot_id=target.id, relay_server_id=target.relay_server_id
        )
        if hit is not None:
            ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
            await self._refuse(session, ctx, "cancelled", self.ANNOUNCED)
            return None
        text = dispatch.round_text(found.job, speaker.display_name or speaker.login_name or "")
        inbound = stand_in(target.id, ctx.task, speaker.platform_user_id)
        intake = Intake(
            target,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            "single",
            inbound.chat_id,
            text,
            self.kind,
        )
        ctx.log.info(
            "assistant_bot_help_opened",
            job_id=str(found.job.id),
            allow_write=ctx.task.payload.get("allow_write") is True,
        )
        return intake, relay, [{"type": "text", "text": text}]

    def _structured_reply(self, ctx: TaskContext) -> bool:
        """终稿是交给平台解析的 JSON：不加详细度、输出格式与回复图片的说明。"""
        return True

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        info = await super()._session_info(session, ctx, intake, backend)
        # 与开流同一事务：这一轮真的开了，求助才算在跑。
        await dispatch.mark_running(session, ctx.task)
        return info

    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        # 先按普通轮次的口径看是不是用提问工具向用户发问（不出声的基类会把它直接改成正常结束）。
        plain = super(SilentTurnHandler, self)._classify(ctx, pre, out)
        if plain.log_status != "ask_user":
            return super()._classify(ctx, pre, out)
        # 没有人能答题：不开选择卡（它会挂在所有者与这位员工的私聊上、把他的下一条消息当成
        # 答案），题面作为所需事项、按 blocked 交回助理。
        asked = [
            str(item.get("question") or "") for item in out.questions if isinstance(item, dict)
        ]
        final = dispatch.blocked_text(pre.writer.pending_text, asked)
        return replace(plain, log_status="success", final_text=ctx.redact(final) or "")

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        await super()._after_finalize(ctx, pre, verdict)
        ok = verdict.task_status == "succeeded"
        answer: dispatch.Answer | None = None
        if ok:
            said, marks = pre.writer.pending_text, pre.writer.boundaries
            final = verdict.final_text.removesuffix(msg("done_suffix", ctx.locale))
            # 约定的 JSON 在终稿里，或在最后一次工具调用之后；都不是就把说过的话原样交回。
            found = dispatch.read_answer(final, said[marks[-1] :] if marks else said, plain=said)
            if found is not None:
                answer = replace(
                    found,
                    answer=ctx.redact(found.answer) or "",
                    needs=tuple(ctx.redact(item) or "" for item in found.needs),
                )
        async with ctx.session_factory() as session:
            row = await dispatch.finish(
                session, ctx.task, ok=ok, answer=answer, error_code=verdict.error_code
            )
            await session.commit()
        ctx.log.info(
            "assistant_bot_help_finished", status=row.status if row is not None else "stale"
        )

    async def _session_busy(self, ctx: TaskContext, intake: Intake) -> None:
        """求助的会话只有它自己的一轮，正常走不到这里：不回提示，记失败并投回。"""
        async with ctx.session_factory() as session:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="session_busy", only_active=True
            )
            await dispatch.fail(session, ctx.task, "session_busy")
            await session.commit()
        ctx.log.warning("assistant_bot_help_session_busy", attempts=ctx.task.attempts)
