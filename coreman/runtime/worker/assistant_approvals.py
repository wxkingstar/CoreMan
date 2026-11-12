"""个人助理审批单的两种任务：批准后的执行（不调模型），以及把结局投回助理会话的一轮。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import execute, service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.config import get_settings
from coreman.core.db.models import AssistantApproval, Bot, RelayServer, User, UserIdentity
from coreman.core.feishu_personal.files import FileRelay
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound

# 审批系统或临时桶连不上时隔多久再试、最多试几次；之后记为失败并告诉助理。
RETRY_SECONDS = 60.0
MAX_ATTEMPTS = 5


class AssistantApprovalExecuteHandler:
    kind = APPROVAL_EXECUTE_KIND

    async def run(self, ctx: TaskContext) -> None:
        settings = get_settings()
        cfg = tower.config(settings)
        try:
            approval_id = uuid.UUID(str(ctx.task.payload.get("approval_id")))
        except ValueError:
            # 载荷读不出来（数据密钥已销毁时认领就会失败，到不了这里）：没有可执行的东西。
            await self._finish(ctx, "failed", "approval_missing")
            return
        try:
            status = await execute.run(
                ctx.session_factory,
                ctx.cipher,
                approval_id,
                ctx.task,
                cfg,
                FileRelay(settings, ctx.cipher),
            )
        except execute.Retry as exc:
            if ctx.task.attempts < MAX_ATTEMPTS:
                async with ctx.session_factory() as session:
                    await tasks.defer(session, ctx.task.id, seconds=RETRY_SECONDS)
                    await session.commit()
                return
            status = await execute.give_up(ctx.session_factory, approval_id, cfg, exc.code)
        ctx.log.info("assistant_approval_executed", status=status)
        await self._finish(ctx, "succeeded", None)

    async def _finish(self, ctx: TaskContext, status: str, code: str | None) -> None:
        async with ctx.session_factory() as session:
            await tasks.finish(
                session, ctx.task.id, status=status, error_code=code, only_active=True
            )
            await session.commit()


class AssistantApprovalResultHandler(ChatTaskHandler):
    """仿 approval_resume：入站事件沿用提交那一轮的，发言者固定为所有者，消息是系统写好的文本。

    不要求还是提交时的那个 relay 会话：助理会话会换新，结果文本自带是哪张单、结局如何。
    """

    kind = APPROVAL_RESULT_KIND

    async def _cancel(self, session: AsyncSession, ctx: TaskContext, code: str) -> None:
        await tasks.finish(session, ctx.task.id, status="cancelled", error_code=code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        try:
            key = uuid.UUID(str(ctx.task.payload.get("approval_id")))
        except ValueError:
            await self._cancel(session, ctx, "approval_result_inactive")
            return None
        row = await session.get(AssistantApproval, key, populate_existing=True)
        if (
            bot is None
            or not bot.enabled
            or not is_assistant(bot)
            or bot.owner_id is None
            or row is None
            or row.bot_id != bot.id
            or row.status != ctx.task.payload.get("status")
            or sealed.is_locked(row)
        ):
            await self._cancel(session, ctx, "approval_result_inactive")
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
            await self._cancel(session, ctx, "approval_owner_changed")
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
            await self._cancel(session, ctx, "approval_result_announced")
            return None
        inbound = await load_inbound(session, ctx.task)
        text = service.result_text(row)
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            inbound.chat_type,
            row.session_key or inbound.chat_id,
            text,
            APPROVAL_RESULT_KIND,
        )
        return intake, relay, [{"type": "text", "text": text}]

    def _needs_content(self) -> bool:
        """消息是系统写好的成品文本，没有媒体要下载。"""
        return False
