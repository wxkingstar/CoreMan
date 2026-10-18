"""approval_resume：Tower 给出决定之后，在原会话里续接提交审批的那一轮。

仿 credential_resume：入站事件沿用来源那一轮，发言者固定为发起人，企业微信的流从创建起
就是主动推送。续接消息是系统写好的成品文本（审批编号、结果、拒绝理由），令牌不在里面——
批准后由 Agent 按 Skill 现取。其余复用 ChatTaskHandler。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import sessions
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.cloud_proxy import resume
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    ChatSession,
    RelayServer,
    Task,
    User,
    UserIdentity,
)
from coreman.core.db.models.cloud_proxy import CloudPendingApproval
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake, Prepared
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


class _SessionChanged(ValueError):
    """开轮时发现提交审批的那个对话已经不在了：不能在另一个会话里续接。"""


async def _reset_by_user(session: AsyncSession, ctx: TaskContext) -> bool:
    """会话是用户自己 /reset 掉的：他知道对话变了，不必再说「对话已经变化」。"""
    task = await session.get(Task, ctx.task.id, populate_existing=True)
    return task is not None and task.cancel_reason == "user_reset"


class ApprovalResumeHandler(ChatTaskHandler):
    kind = resume.RESUME_KIND

    async def run(self, ctx: TaskContext) -> None:
        try:
            await super().run(ctx)
        except _SessionChanged:
            # 开轮事务已随异常回滚，另开事务告诉发起人、把任务按取消结掉
            async with ctx.session_factory() as session:
                bot = await session.get(Bot, ctx.task.bot_id)
                row = await session.get(
                    CloudPendingApproval, str(ctx.task.payload.get("external_id"))
                )
                if bot is not None and row is not None and not await _reset_by_user(session, ctx):
                    inbound = await load_inbound(session, ctx.task)
                    await resume.skipped_notice(session, bot, row, inbound.chat_id)
                await tasks.finish(
                    session,
                    ctx.task.id,
                    status="cancelled",
                    error_code="approval_resume_session_changed",
                    only_active=True,
                )
                await session.commit()

    async def _cancel(self, session: AsyncSession, ctx: TaskContext, code: str) -> None:
        await tasks.finish(session, ctx.task.id, status="cancelled", error_code=code)

    async def _skip(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        bot: Bot,
        row: CloudPendingApproval,
        code: str,
    ) -> None:
        if not await _reset_by_user(session, ctx):
            inbound = await load_inbound(session, ctx.task)
            await resume.skipped_notice(session, bot, row, inbound.chat_id)
        await self._cancel(session, ctx, code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        external_id = str(ctx.task.payload.get("external_id") or "")
        row = (
            await session.get(
                CloudPendingApproval, external_id, with_for_update=True, populate_existing=True
            )
            if external_id
            else None
        )
        if (
            bot is None
            or not bot.enabled
            or row is None
            or row.user_id is None
            or row.resume_task_id != ctx.task.id
            # Agent 已自己取令牌执行（executed）等情形：这条续接过时了
            or row.status not in resume.RESUME_STATUSES
        ):
            await self._cancel(session, ctx, "approval_resume_inactive")
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == row.user_id, UserIdentity.platform == bot.platform)
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, row.user_id, populate_existing=True)
        allowed = set(
            await session.scalars(
                select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id)
            )
        )
        if (
            speaker.user_id != row.user_id
            or user is None
            or user.status != "active"
            or (allowed and row.user_id not in allowed)
        ):
            await self._cancel(session, ctx, "approval_owner_changed")
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        current = await session.get(ChatSession, (bot.id, row.session_key), populate_existing=True)
        if current is None or str(current.relay_session_id) != row.origin_relay_session_id:
            # 提交之后对话被重置或切走：续接会落进另一个对话，上下文对不上
            await self._skip(session, ctx, bot, row, "approval_resume_session_changed")
            return None
        if await resume.newer_turn_by_other(session, row):
            # 决定回来之前会话里来了别人的新请求：续接会接到它上面
            await self._skip(session, ctx, bot, row, "approval_resume_superseded")
            return None
        hit = await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id)
        if hit is not None:
            # 与 chat 流水线同一口径：维护期不开轮
            ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
            await self._cancel(session, ctx, "approval_resume_announced")
            return None
        inbound = await load_inbound(session, ctx.task)
        text = resume.resume_text(
            approval_id=str(row.approval_id),
            status=row.status,
            product=row.product,
            action=row.action,
            reason=row.reason or "",
        )
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            inbound.chat_type,
            row.session_key,
            text,
            resume.RESUME_KIND,
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        """开轮时（已持 bot 行锁）再确认一次：续接只进提交审批的那个 relay 会话。

        排队期间换模型、换运行时、清会话都会让 get_or_create 悄悄建一个空会话。
        """
        row = await session.get(CloudPendingApproval, str(ctx.task.payload.get("external_id")))
        info = await super()._session_info(session, ctx, intake, backend)
        if row is None or str(info.relay_session_id) != row.origin_relay_session_id:
            raise _SessionChanged("approval resume session changed before dispatch")
        return info

    def _needs_content(self) -> bool:
        """续接消息是系统写好的成品文本，没有媒体要下载。"""
        return False

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """企业微信：没有可用的 req_id，流从创建就走 outbox 主动推送；飞书沿用来源的回复上下文。"""
        if intake.bot.platform != "wecom":
            return {}
        now = datetime.now(UTC).isoformat()
        return {
            "delivery_mode": "proactive",
            "background_state": {
                "mode": "proactive",
                "offset": 0,
                "finish_suffix": "",
                "switched_at": now,
            },
            "reply_context": {
                "gateway_instance": ctx.instance_id,
                "received_at": now,
                "chat_type": intake.chat_type,
                "chat_id": intake.chat_id,
            },
        }

    def _after_supervisor(self, pre: Prepared) -> None:
        if pre.intake.bot.platform == "wecom":
            pre.supervisor.start_proactive(pre.started_clock)
