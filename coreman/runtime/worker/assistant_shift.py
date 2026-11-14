"""所有者在私聊里临时切换上下班：发「上班」或「下班」，平台直接处理并回一句，不经模型。

与「停用助理」同一处截下（入站阶段认出发言人之后，早于公告、待答与运行时检查），只截私聊里
整条消息就是命令的情况；别人发的不回应、不生效。到期时刻的算法在 `settings.clock_in` /
`clock_out`；到点不用清理，`settings.on_shift` 只认没过期的临时切换。

同一处还管冷启动的两件事：冷启动进行中所有者发「跳过」，整个跳过；2F 上线前开通的助理没有
冷启动记录，所有者下一次来私聊时开始（那条消息照常交给模型）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import cold_start
from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import outbox, tasks
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.prompting import Speaker
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

ON = frozenset({"上班", "/上班"})
OFF = frozenset({"下班", "/下班"})
COMMANDS = ON | OFF
# 只在冷启动进行中才截下；其余时候是一句普通的话。
SKIP = frozenset({"跳过", "/跳过"})
# 不并进正在跑的一轮的词：留给它们自己那一轮去截下（`chat/queued_batch.py`）。
RESERVED = COMMANDS | SKIP
_WEEKDAYS = "一二三四五六日"
NOT_WATCHING = "冷启动还没做完，我还没开始看消息；做完或发「跳过」之后才开始。"


def when(prefs: AssistantSettings, at: datetime, now: datetime) -> str:
    """到期时刻在所有者时区里的说法。"""
    zone = assistant_settings.zone(prefs)
    local, today = at.astimezone(zone), now.astimezone(zone).date()
    clock = local.strftime("%H:%M")
    days = (local.date() - today).days
    if days == 0:
        return f"今天 {clock}"
    if days == 1:
        return f"明天 {clock}"
    return f"{local.month} 月 {local.day} 日（周{_WEEKDAYS[local.isoweekday() - 1]}）{clock}"


def _reply(prefs: AssistantSettings, on: bool, until: datetime | None, now: datetime) -> str:
    """`until` 为 None 是全天候时说「上班」：没有到期时刻，只是取消了「下班」。"""
    if until is None:
        text = "已上班，全天候看你的飞书消息。"
    elif on:
        text = f"已上班，到{when(prefs, until, now)}为止，之后按你的工作时段。"
    else:
        text = (
            f"已下班，{when(prefs, until, now)}恢复。这期间我不看你的飞书消息，"
            "你在这里说话我照常回应。"
        )
    if prefs.cold_start_done_at is None:
        text += "\n" + NOT_WATCHING
    return text


async def _switch(
    session: AsyncSession, ctx: TaskContext, bot: Bot, owner: bool, chat_id: str, word: str
) -> None:
    """「上班」「下班」：所有者的就地生效并回一句，别人的就此结束。"""
    result = "shift_not_owner"
    if owner and bot.assistant_state != "active":
        # 开通没走完或正在停用：没有可切换的东西，也不回话（停用流程自己会告知）。
        result = "shift_unavailable"
    elif owner:
        now = utcnow()
        prefs = await assistant_settings.get(session, bot.id)
        await session.refresh(prefs, with_for_update=True)
        on = word in ON
        if on or assistant_settings.on_shift(prefs, now):
            # 今天上过班：下班编译凭它触发，不等 5 秒一次的循环来记；前一天还欠着整理时不覆盖。
            assistant_settings.mark_worked(prefs, now)
        until = (
            assistant_settings.clock_in(prefs, now)
            if on
            else assistant_settings.clock_out(prefs, now)
        )
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"assistant-shift:{ctx.task.id}",
            target={"chat_id": chat_id},
            payload={"markdown": _reply(prefs, on, until, now)},
        )
        result = "shift_on" if on else "shift_off"
    await tasks.finish(
        session,
        ctx.task.id,
        status="succeeded",
        result={"assistant": result},
        only_active=True,
        bot_id=bot.id,
    )


async def intercept(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    speaker: Speaker,
    chat_id: str,
    text: str,
) -> bool:
    """个人助理私聊里的一条消息：是平台直接处理的命令就处理掉并返回真，其余返回假。

    写在调用方的事务里，由调用方提交。普通消息多一次按主键读设置行（只有个人助理走到这里）。
    """
    word = text.strip()
    owner = speaker.user_id is not None and speaker.user_id == bot.owner_id
    if word in COMMANDS:
        await _switch(session, ctx, bot, owner, chat_id, word)
        return True
    if not owner or bot.assistant_state != "active":
        return False
    prefs = await session.get(AssistantSettings, bot.id)
    if prefs is None:
        # 还没有设置行（没记下身份的旧助理的第一轮）：跑不了平台来源的轮次，冷启动不开始。
        return False
    now = utcnow()
    if word in SKIP and cold_start.pending(prefs):
        await cold_start.complete(session, bot, prefs, "skipped", now)
        await tasks.finish(
            session,
            ctx.task.id,
            status="succeeded",
            result={"assistant": "cold_start_skipped"},
            only_active=True,
            bot_id=bot.id,
        )
        return True
    # 2F 上线前开通的助理：所有者下一次来私聊时开始冷启动（已开始过的什么都不做）。
    await cold_start.begin(session, bot.id, now)
    return False
