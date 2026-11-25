"""让 AI 员工开工：人派给它的任务等人叫它或定时领取（spec §5.1），验收退回后自动重跑。

开工那一轮写一条合成的入站事件（消息 id `work:<任务 id>:<序号>`，飞书上没有这条消息），
在代表人和这位员工的私聊里说一句开工了，再按任务会话 `work:<编号>` 入队一个 chat 任务。
这一轮以代表人的身份做，worker 里 `work.resolve` 会再核对一遍。
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import WORK_SYNTHETIC_PREFIX
from coreman.core.api_tasks.service import feishu_user_id
from coreman.core.assistants import sealed
from coreman.core.bus import outbox, tasks
from coreman.core.chat.dispatch import work_session_key
from coreman.core.db.models import (
    Bot,
    InboundEvent,
    User,
    UserReached,
    WorkTask,
    WorkTaskRun,
)
from coreman.core.wecom.messages import InboundMessage, Sender, TextPart
from coreman.core.work_tasks import service
from coreman.core.work_tasks.service import WorkTaskError, add_event, clip, name_of, now, usable_bot

# chat：人在对话里叫它；cron：定时领取；rerun：验收退回；comment：停下后人补了话；
# subtask：上级 AI 员工派的子任务；resume：等的子任务都有结果了；handoff：别的员工整件转交过来。
TRIGGERS = ("chat", "cron", "rerun", "comment", "subtask", "resume", "handoff")
# 不需要「待领取」状态就能开工的触发方式。
_RESTART = ("rerun", "resume")


async def start(
    session: AsyncSession,
    row: WorkTask,
    *,
    bot: Bot,
    trigger: str,
    triggered_by: dict[str, Any],
    note: str = "",
    silent: bool = False,
) -> None:
    """排一轮执行员工的对话。`silent`：子任务这样没人在看的一轮，回复不发到任何会话。

    回复发到哪：不出声的一轮不发；调度员交来的任务回到原群；其余发到代表人和它的私聊。
    """
    if row.origin_ref:
        raise WorkTaskError(409, f"{row.code} 是自动记下的记录，不能领取")
    if row.assignee_bot_id != bot.id:
        raise WorkTaskError(403, f"{row.code} 不是派给「{bot.name}」的任务")
    if trigger in _RESTART:
        if row.status in ("shadow", "done", "cancelled"):
            raise WorkTaskError(409, f"{row.code} 已结束")
    elif not ((row.status == "todo" and row.claimed_at is None) or service.restartable(row)):
        raise WorkTaskError(409, f"{row.code} 已经开工或已结束，不用再领取")
    if row.principal_user_id is None:
        raise WorkTaskError(409, f"{row.code} 没有代表人，没法以谁的身份去做")
    principal = await session.get(User, row.principal_user_id)
    if principal is None or principal.status != "active":
        raise WorkTaskError(409, f"{row.code} 的代表人已停用")
    await usable_bot(session, bot.id, principal.id)
    pid = await feishu_user_id(session, principal.id)
    if not pid:
        raise WorkTaskError(409, f"{name_of(principal)}没有飞书身份，没法以他的身份去做")
    chat_type = "single"
    if silent:
        chat_id = f"work:{row.code}"
    elif row.source == "dispatch" and row.platform == "feishu" and row.chat_id:
        chat_id, chat_type = row.chat_id, "group"
    else:
        reached = await session.get(UserReached, (bot.id, principal.id))
        if reached is None or not reached.platform_chat_id:
            raise WorkTaskError(
                409,
                f"{name_of(principal)}还没有在飞书私聊过「{bot.name}」，结果没处回报；"
                f"请他先私聊「{bot.name}」一次",
            )
        chat_id = reached.platform_chat_id
    seq = int(
        await session.scalar(
            select(func.count()).select_from(WorkTaskRun).where(WorkTaskRun.work_task_id == row.id)
        )
        or 0
    )
    message_id = f"{WORK_SYNTHETIC_PREFIX}{row.id}:{seq + 1}:{trigger}"
    text = f"开始处理任务 {row.code}：{row.title}" + (f"\n补充：{note}" if note else "")
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender=Sender(platform_user_id=pid),
        message_id=message_id,
        mentions_bot=True,
        parts=[TextPart(text=text)],
        reply_context={"chat_id": chat_id, "chat_type": chat_type},
    )
    sealed_values = await sealed.seal_values(
        session,
        InboundEvent,
        bot.id,
        {"payload": message.model_dump(mode="json"), "reply_context": message.reply_context},
    )
    event_id = await session.scalar(
        insert(InboundEvent)
        .values(
            bot_id=bot.id,
            platform="feishu",
            platform_msg_id=message_id,
            kind="message",
            chat_type=chat_type,
            chat_id=chat_id,
            sender_platform_user_id=pid,
            sender_open_id=None,
            payload=sealed_values["payload"],
            reply_context=sealed_values["reply_context"],
        )
        .returning(InboundEvent.id)
    )
    if not silent and (row.platform is None or row.chat_id is None):
        row.platform, row.chat_id = "feishu", chat_id
    if row.claimed_at is None:
        row.claimed_at = now()
    row.status, row.ask, row.closed_at = "waiting", None, None
    row.waiting_on = {"kind": "queue", "human_text": f"排队等{bot.name}开始"}
    who = str(triggered_by.get("name") or "")
    line = {
        "chat": f"{who}叫{bot.name}开工",
        "cron": f"{bot.name}按定时任务领取",
        "rerun": f"交回{bot.name}重做",
        "comment": f"{who}补充后交回{bot.name}接着做",
        "subtask": f"{bot.name}接手",
        "resume": f"等的事都有结果了，{bot.name}接着做",
        "handoff": f"转交给{bot.name}",
    }.get(trigger, f"{bot.name}开工")
    add_event(
        session,
        row,
        kind="claimed" if trigger != "rerun" else "rerun",
        actor_type="system",
        actor_id=None,
        human_text=line,
        detail={"trigger": trigger, "triggered_by": triggered_by, "note": note},
    )
    if chat_type == "single" and not silent:
        await _announce(session, row, bot, chat_id, principal, pid, event_id, line)
    await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="chat",
            user_id=principal.id,
            inbound_event_id=event_id,
            session_key=work_session_key(row.code),
            dedupe_key=f"work:{row.id}:{event_id}",
            payload={
                "work_task_id": row.id,
                "work_note": note,
                "work_trigger": trigger,
                "work_silent": silent,
                "work_triggered_by": triggered_by,
                "bot_key": bot.bot_key,
                "platform_user_id": pid,
                "serialize_session": True,
            },
        ),
    )


async def _announce(
    session: AsyncSession,
    row: WorkTask,
    bot: Bot,
    chat_id: str,
    principal: User,
    pid: str,
    event_id: int | None,
    line: str,
) -> None:
    """代表人的私聊里说一句开工了：私聊里冒出来的回复要让人知道是哪件事。"""
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"work-task:{row.id}:{event_id}:trigger",
        target={
            "chat_id": chat_id,
            "recipient_user_id": str(principal.id),
            "recipient_platform_user_id": pid,
        },
        payload={"markdown": clip(f"**{row.code}** {line}：{row.title}", 200)},
    )


async def answer(
    session: AsyncSession,
    row: WorkTask,
    user: User,
    *,
    ask_id: str,
    option: str,
    text: str,
    channel: Literal["web", "feishu"],
) -> None:
    """网页和飞书卡片共用：回答，验收退回给 AI 员工的立刻排一轮重做。"""
    result = await service.answer(
        session, row, user, ask_id=ask_id, option=option, text=text, channel=channel
    )
    if result.rerun_note is not None and row.assignee_bot_id:
        bot = await session.get(Bot, row.assignee_bot_id)
        if bot is None:
            return
        try:
            async with session.begin_nested():
                await start(
                    session,
                    row,
                    bot=bot,
                    trigger="rerun",
                    triggered_by={"user_id": str(user.id), "name": name_of(user)},
                    note=result.rerun_note,
                )
        except WorkTaskError as exc:
            # 交不回去（例如代表人没私聊过它）：退回照样生效，改成等人叫它开工。
            row.status, row.claimed_at, row.start_mode = "todo", None, "manual"
            add_event(
                session,
                row,
                kind="rerun_failed",
                actor_type="system",
                actor_id=None,
                human_text=f"没能自动交回{bot.name}重做：{exc.message}",
                detail={"reason": exc.message, "note": result.rerun_note},
            )
