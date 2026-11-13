"""交回事项的一轮：平台把盯消息攒下的条目写成整理稿，在助理的主会话里跑一轮。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import delivery, handover
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.db.models import AssistantSettings, Bot
from coreman.runtime.worker.chat.models import Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler


class AssistantHandoverHandler(PlatformTurnHandler):
    """文本取自绑在这一轮上的收件箱条目；收尾时按这一轮的结局给条目记账。

    `_text` 只读：基类在占会话之前调它，让路放回队列后每次重新认领都会再调一次。条目的
    绑定在入队时就定了（`delivery.dispatch`），这里不绑新的、不改状态。没跑到收尾的结局
    （开流前被取消、暂停、让路放弃）由 scheduler 的 `delivery.reconcile` 记账。
    """

    kind = HANDOVER_KIND
    INACTIVE = "handover_inactive"
    OWNER_CHANGED = "handover_owner_changed"
    ANNOUNCED = "handover_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        rows = await delivery.bound(session, ctx.task)
        if not rows:
            return None
        prefs = await session.get(AssistantSettings, bot.id)
        zone = delivery.zone_named(prefs.timezone if prefs is not None else None)
        ctx.log.info("assistant_handover_opened", items=len(rows))
        return handover.render(rows, zone)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        await super()._after_finalize(ctx, pre, verdict)
        ok = verdict.task_status == "succeeded"
        async with ctx.session_factory() as session:
            settled = await delivery.settle(session, ctx.task, ok)
            await session.commit()
        ctx.log.info("assistant_handover_settled", ok=ok, items=settled)
