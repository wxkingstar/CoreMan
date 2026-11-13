"""冷启动的口吻样本那一轮：文本是平台拉好的样本，跑在这位助理的另一个会话里。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import voice
from coreman.core.assistants.kinds import COLD_START_KIND
from coreman.core.db.models import Bot
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import SilentTurnHandler


class AssistantColdStartHandler(SilentTurnHandler):
    """不出声：归纳口吻的这一轮没有人在等，它的话不发到私聊；冷启动做完时平台另发一句确认。"""

    kind = COLD_START_KIND
    # 不是主会话，本来就不参与每日换新；写明免得以后改了判断条件把它算进去。
    DAILY_SESSIONS = False

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        """载荷里的样本就是全部输入；不够数就没有这一轮。无副作用。"""
        found = ctx.task.payload.get("samples")
        given = found if isinstance(found, list) else []
        samples = [text for text in given if isinstance(text, str) and text.strip()]
        if len(samples) < voice.MIN_MESSAGES:
            return None
        return voice.render(samples[: voice.MAX_MESSAGES])
