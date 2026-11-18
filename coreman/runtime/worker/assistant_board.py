"""所有者在私聊里发「看板」：平台直接回一张事项总表卡片，不经模型、不开轮次。

与「上班」「下班」一样在入站阶段认出发言人后截下（`chat/intake.py`），只截私聊里的普通聊天消息；
不是所有者发的就此结束、不回话。开通没走完或设置行还没有身份时不截，交给后面的流程。
「不开 G003」同样在这里截下：平台直接记下所有者不开，回一句确认。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import digest, gaps
from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import outbox, tasks
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.prompting import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

COMMANDS = frozenset({"看板", "/看板", "有什么事"})
DECLINE_REPLIES = {
    "gap_declined": "好的，{label} 不开了，助理不会再为它提醒你。",
    "gap_not_found": "没有找到 {label}。",
    "gap_resolved": "{label} 已经可以用了，不用再处理。",
}


def is_command(text: str) -> bool:
    """平台自己截下的私聊命令：「看板」与「不开 G003」。合并排队消息时不能把它们并进去。"""
    clean = text.strip()
    return clean in COMMANDS or gaps.decline_seq(clean) is not None


def keeps_own_turn(text: str) -> bool:
    """排队合并时要留给自己那一轮的：平台截下的命令，加上「现在处理 F012」「开通 G003」
    （那一轮要附上对应的说明，并进别的轮次就丢了）。"""
    clean = text.strip()
    return (
        is_command(clean) or gaps.open_seq(clean) is not None or digest.now_seq(clean) is not None
    )


async def _decline(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, seq: int, key: str
) -> str:
    try:
        await gaps.decline(session, bot.id, seq)
        result = "gap_declined"
    except gaps.GapError as exc:
        result = exc.code
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-gap-decline:{bot.id}:{key}",
        target={"chat_id": prefs.chat_id},
        payload={"markdown": DECLINE_REPLIES[result].format(label=gaps.label(seq))},
    )
    return result


async def intercept(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    speaker: Speaker,
    chat_id: str,
    text: str,
) -> bool:
    """是「看板」或「不开 G003」就处理掉并返回真，其余返回假。写在调用方的事务里，由调用方提交。

    `chat_id` 与另两个拦截函数同形；卡片发到设置行记下的私聊。
    """
    clean = text.strip()
    declined = gaps.decline_seq(clean)
    if clean not in COMMANDS and declined is None:
        return False
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    prefs = await session.get(AssistantSettings, bot.id) if owner else None
    if owner and (
        bot.assistant_state != "active" or prefs is None or not assistant_settings.identified(prefs)
    ):
        return False
    result = "board_not_owner"
    if owner and prefs is not None:
        if declined is None:
            await digest.send_board(session, bot, prefs, utcnow(), key_suffix=str(ctx.task.id))
            result = "board_sent"
        else:
            result = await _decline(session, bot, prefs, declined, str(ctx.task.id))
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": result},
        only_active=True,
        bot_id=bot.id,
    )
    return True
