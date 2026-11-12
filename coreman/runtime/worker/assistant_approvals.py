"""个人助理审批单的两种任务：批准后的执行（不调模型），以及把结局投回助理会话的一轮。"""

from __future__ import annotations

import uuid

from coreman.core.assistants.approvals import execute, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND
from coreman.core.bus import tasks
from coreman.core.config import get_settings
from coreman.core.feishu_personal.files import FileRelay
from coreman.runtime.worker.context import TaskContext

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
