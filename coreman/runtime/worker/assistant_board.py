"""所有者在私聊里发「看板」：平台直接回一张事项总表卡片，不经模型、不开轮次。

与「上班」「下班」一样在入站阶段认出发言人后截下（`chat/intake.py`），只截私聊里的普通聊天消息；
不是所有者发的就此结束、不回话。开通没走完或设置行还没有身份时不截，交给后面的流程。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import digest
from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.prompting import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"看板", "/看板", "有什么事"})


async def intercept(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    speaker: Speaker,
    chat_id: str,
    text: str,
) -> bool:
    """是「看板」就处理掉并返回真，其余返回假。写在调用方的事务里，由调用方提交。

    `chat_id` 与另两个拦截函数同形；卡片发到设置行记下的私聊。
    """
    if text.strip() not in COMMANDS:
        return False
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    prefs = await session.get(AssistantSettings, bot.id) if owner else None
    if owner and (
        bot.assistant_state != "active" or prefs is None or not assistant_settings.identified(prefs)
    ):
        return False
    result = "board_not_owner"
    if owner and prefs is not None:
        await digest.send_board(session, bot, prefs, utcnow(), key_suffix=str(ctx.task.id))
        result = "board_sent"
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": result},
        only_active=True,
        bot_id=bot.id,
    )
    return True
