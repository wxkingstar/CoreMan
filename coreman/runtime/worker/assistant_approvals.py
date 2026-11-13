"""个人助理审批单的两种任务：批准后的执行（不调模型），以及把结局投回助理会话的一轮。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import execute, service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.config import get_settings
from coreman.core.db.models import AssistantApproval, Bot
from coreman.core.feishu_personal.files import FileRelay
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler

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


class AssistantApprovalResultHandler(PlatformTurnHandler):
    """仿 approval_resume：发言者固定为所有者，消息是系统写好的文本。

    入站事件沿用提交那一轮的；审批单是在平台来源的轮次里提交的（没有入站事件）时用替身，回复
    发到所有者的私聊。不要求还是提交时的那个 relay 会话：助理会话会换新，结果文本自带是哪张单、
    结局如何。
    """

    kind = APPROVAL_RESULT_KIND
    INACTIVE = "approval_result_inactive"
    OWNER_CHANGED = "approval_owner_changed"
    ANNOUNCED = "approval_result_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        try:
            key = uuid.UUID(str(ctx.task.payload.get("approval_id")))
        except ValueError:
            return None
        row = await session.get(AssistantApproval, key, populate_existing=True)
        if (
            row is None
            or row.bot_id != bot.id
            or row.status != ctx.task.payload.get("status")
            or sealed.is_locked(row)
        ):
            return None
        return service.result_text(row)
