"""平台发起的一轮的处理器基类：发言者固定为所有者，消息是平台写好的文本，没有入站内容。

交回事项、子任务及其结果、下班编译、审批结果都是这样的一轮：流水线（会话、提示词、个人工具、
沙箱、转录、收尾）与所有者私聊的一轮完全相同，只有入站解析不同。子类给出任务类型与这一轮的
文本；带入站事件的任务（审批结果回合沿用提交那一轮的）照旧用它，不带的用一个不落库的替身。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns, runtime
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import streams, tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.rate_limit import is_rate_limited
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    RelayServer,
    Task,
    User,
    UserIdentity,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat.models import Outcome, Prepared, Verdict
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


def stand_in(bot: Bot, row: AssistantSettings, task: Task) -> InboundEvent:
    """没有入站事件的一轮用的替身：不加入会话、不落库，只给流水线提供会话坐标。

    回复上下文里只有所有者的私聊，没有被回复的消息：这一轮的回复作为一条新消息发过去。
    """
    return InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"platform-turn:{task.id}",
        kind="message",
        chat_type="single",
        chat_id=row.chat_id,
        sender_platform_user_id=row.platform_user_id,
        sender_open_id=row.open_id,
        payload={},
        reply_context={"chat_id": row.chat_id, "chat_type": "single"},
        received_at=utcnow(),
    )


class PlatformTurnHandler(ChatTaskHandler):
    """子类设置 `kind` 并实现 `_text`；其余与私聊一轮相同。"""

    kind = ""
    # 取消任务时写的错误代码：这一轮已经没有意义、所有者换了人或账号停用、运行时有公告。
    INACTIVE = "platform_turn_inactive"
    OWNER_CHANGED = "platform_owner_changed"
    ANNOUNCED = "platform_turn_announced"
    # 让路：同会话有别的任务时放回队列，等待逐次加倍；认领这么多次还轮不到就记失败，不私信。
    YIELDED = "platform_turn_yielded"
    YIELD_RETRY_SECONDS = 15.0
    YIELD_RETRY_MAX_SECONDS = 300.0
    YIELD_MAX_ATTEMPTS = 10

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        """这一轮交给模型的文本；返回 None 表示这一轮已经没有意义，任务按 `INACTIVE` 取消。"""
        raise NotImplementedError

    async def _cancel(self, session: AsyncSession, ctx: TaskContext, code: str) -> None:
        await tasks.finish(session, ctx.task.id, status="cancelled", error_code=code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        if bot is None or not bot.enabled or not is_assistant(bot) or bot.owner_id is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        text = await self._text(session, ctx, bot)
        if text is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform="feishu",
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, bot.owner_id, populate_existing=True)
        if speaker.user_id != bot.owner_id or user is None or user.status != "active":
            await self._cancel(session, ctx, self.OWNER_CHANGED)
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        hit = await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id)
        if hit is not None:
            ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
            await self._cancel(session, ctx, self.ANNOUNCED)
            return None
        if ctx.task.inbound_event_id is not None:
            inbound = await load_inbound(session, ctx.task)
        else:
            found = await self._stand_in(session, ctx, bot)
            if found is None:
                return None
            inbound = found
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            inbound.chat_type,
            ctx.task.session_key or inbound.chat_id,
            text,
            self.kind,
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _stand_in(
        self, session: AsyncSession, ctx: TaskContext, bot: Bot
    ) -> InboundEvent | None:
        """没有入站事件的一轮的替身；这一轮不该跑时结掉任务并返回 None。"""
        row = await session.get(AssistantSettings, bot.id)
        if (
            row is None
            or not platform_turns.is_platform(ctx.task)
            or not platform_turns.owns_session(row, ctx.task.session_key)
        ):
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        reason = await runtime.turn_blocker(session, bot)
        if reason is not None:
            # 没有人在等这一轮：暂停期间不为平台自己发起的任务再私信所有者，只结掉任务。
            ctx.log.warning("assistant_paused", reason=reason)
            await tasks.finish(session, ctx.task.id, status="failed", error_code="assistant_paused")
            return None
        return stand_in(bot, row, ctx.task)

    async def _take_session(self, ctx: TaskContext, intake: Intake) -> bool:
        """无人在场的一轮不取消同会话的任务：没人在等它，所有者的消息优先。

        同会话有别的任务在跑、或所有者的消息在排队时，自己放回队列稍后再认领。反过来所有者
        一发消息，这一轮照旧被那条消息取消（`ChatTaskHandler._take_session`）。带入站事件的
        审批结果回合同样如此：那条入站事件是提交那一轮留下的，所有者此刻不在场。
        """
        if not platform_turns.unattended(ctx.task):
            return await super()._take_session(ctx, intake)
        async with ctx.session_factory() as session:
            if not await tasks.in_the_way(
                session, intake.bot.id, intake.session_key, task_id=ctx.task.id
            ):
                return True
            if ctx.task.attempts >= self.YIELD_MAX_ATTEMPTS:
                await tasks.finish(
                    session,
                    ctx.task.id,
                    status="failed",
                    error_code=self.YIELDED,
                    only_active=True,
                )
                await session.commit()
                ctx.log.warning("platform_turn_yield_gave_up", attempts=ctx.task.attempts)
                return False
            seconds = min(
                self.YIELD_RETRY_SECONDS * 2 ** max(ctx.task.attempts - 1, 0),
                self.YIELD_RETRY_MAX_SECONDS,
            )
            deferred = await tasks.defer(session, ctx.task.id, seconds=seconds)
            await session.commit()
        if deferred:
            ctx.log.info("platform_turn_yielded", attempts=ctx.task.attempts, seconds=seconds)
        else:
            # 没放回去：刚被所有者的消息取消，按取消收尾。
            await self._cancelled_before_open(ctx)
        return False

    def _needs_content(self) -> bool:
        """消息是平台写好的成品文本，没有媒体要下载。"""
        return False


class SilentTurnHandler(PlatformTurnHandler):
    """不出声的一轮：回复、进度、提问、额度告示、完成提醒都不往任何会话里发。

    子任务、冷启动的口吻样本这样的轮次没有人在等，它们的会话键（`<私聊>#job:…`）也不是一个能
    发消息的会话；结局由平台另行带回（子任务的结论投回主会话，冷启动做完时平台发一句确认）。
    任务类型同时要登记在 `kinds.SILENT_KINDS`：收尸按它判断，不往会话里发「进程没了」。

    不出声的几处都在这里：流的回复上下文带 `_silent`（网关不推、流恢复不补发）、看护器不推
    进度与超时通知、不发长任务完成提醒、不自己推终稿、不开选择卡、触限时不发切换卡、会话忙时
    不回提示。
    """

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        return {"reply_context": {**intake.inbound.reply_context, "_silent": True}}

    def _after_supervisor(self, pre: Prepared) -> None:
        pre.supervisor.silent = True

    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        verdict = super()._classify(ctx, pre, out)
        if verdict.log_status == "ask_user":
            # 没有人能答题：不开选择卡（它会挂在所有者的私聊上），题面留在终稿里。
            return replace(verdict, log_status="success")
        if verdict.log_status in ("success", "error") and is_rate_limited(verdict.final_text):
            # 终稿是「额度用完」的告示，不是这一轮该有的结果：按失败收尾。
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
        """跑多久都不往私聊里发完成提醒：结局由平台另行带回。"""

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
        """这个会话里上一轮停不下来：不回提示，只把这一轮记失败（子任务由对账收口）。"""
        async with ctx.session_factory() as session:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="session_busy", only_active=True
            )
            await session.commit()
        ctx.log.warning("silent_turn_session_busy", kind=self.kind, attempts=ctx.task.attempts)
