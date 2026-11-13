"""下班编译那一轮：文本由平台按当天的活动生成，跑在主会话里。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import compiling
from coreman.core.assistants.kinds import COMPILE_KIND
from coreman.core.db.models import AssistantSettings, Bot
from coreman.runtime.worker.chat.models import Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler


class AssistantCompileHandler(PlatformTurnHandler):
    kind = COMPILE_KIND
    # 「上班」延到次日零点才下班时，这一轮落在第二天：接着前一天的会话整理，不换新会话。
    DAILY_SESSIONS = False

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        """按载荷里的窗口重新汇总一次活动；无副作用（让路后每次重新认领都会再调）。"""
        payload = ctx.task.payload
        try:
            day = date.fromisoformat(str(payload.get("day")))
            since = datetime.fromisoformat(str(payload.get("since")))
        except ValueError:
            return None
        prefs = await session.get(AssistantSettings, bot.id)
        if prefs is None:
            return None
        found = await compiling.activity(session, bot.id, prefs.chat_id, since)
        return None if found.empty else compiling.render(day, found)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        """整理成功了才推进活动窗口：失败、被取消的那一天，活动留给下一次编译。"""
        await super()._after_finalize(ctx, pre, verdict)
        if verdict.task_status != "succeeded":
            return
        try:
            until = datetime.fromisoformat(str(ctx.task.payload.get("until")))
        except ValueError:
            return
        async with ctx.session_factory() as session:
            prefs = await session.get(AssistantSettings, ctx.task.bot_id, with_for_update=True)
            if prefs is not None and (prefs.compiled_at is None or prefs.compiled_at < until):
                prefs.compiled_at = until
            await session.commit()
