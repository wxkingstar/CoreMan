"""AskUserQuestion 待答状态的推进（spec §8.5）：下一题，或答完一轮后交给提交任务。

两条入口共用这一段：用户直接发文字回答（`ChatTaskHandler._pending_answer`）与用户在卡片上
点选提交（`CardActionHandler`）。两边算出的「第几题、答了什么」形状相同，往后该发什么、
该不该提交也就必须相同——分成两份实现，迟早会在「最后一题之后做什么」上走岔。

推进只写 outbox 不直接调企微：卡片与 brief 都要按顺序投递、失败要重试，这是出站箱的活。
幂等键带上状态 id 与题号，同一题被重复推进（用户连点两下卡片）也只会发一份。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import interactions
from coreman.core.db.models import Bot, InteractionState, OutboxItem
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import answered_card, choice_card, question_brief, question_task_id
from coreman.runtime.worker.context import TaskContext

# 选了「其他」之后等用户打字的时限：超过这么久再来的消息算新话题，不再吞成上一题的答案。
FREE_TEXT_WAIT_SECONDS = 1800


def record_answer(state: dict[str, Any], index: int, answer: str) -> list[str]:
    """把第 `index` 题的答案落进 answers 副本；中间跳过的题补空串，题号与下标才对得上。"""
    answers = [str(a) for a in (state.get("answers") or [])]
    answers += [""] * (index + 1 - len(answers))
    answers[index] = answer
    return answers


async def advance_choice(
    session: AsyncSession,
    *,
    ctx: TaskContext,
    bot: Bot,
    st: InteractionState,
    questions: list[dict[str, Any]],
    answers: list[str],
    index: int,
    chat_id: str,
    chat_type: str,
    platform_user_id: str,
    icon_url: str,
) -> None:
    """第 `index` 题答完了：写回状态，再发下一题或排一个提交任务。

    状态一律用 `patch_state` 合并：并发的限流切换可能同时往同一条状态上写别的键，整块
    覆写会把它刚写进去的那一份抹掉。
    """
    total = len(questions)
    nxt = index + 1
    if bot.platform == "feishu" and ctx.task.kind == "chat" and st.state.get("waiting_for_text"):
        # Feishu permits updating the original card after a separate text reply.
        # Resolve only a card actually sent by this bot into the same chat.
        task_id = question_task_id(st.task_id_prefix or "", index)
        sent = await session.scalar(
            select(OutboxItem).where(
                OutboxItem.bot_id == bot.id,
                OutboxItem.kind == "send",
                OutboxItem.status == "sent",
                OutboxItem.target["chat_id"].astext == chat_id,
                OutboxItem.payload["card"]["task_id"].astext == task_id,
            )
        )
        message_id = sent.payload.get("_feishu_message_id") if sent else None
        if message_id and 0 <= index < total:
            await outbox.add(
                session,
                bot_id=bot.id,
                platform=bot.platform,
                kind="card_update",
                dedupe_key=f"{st.id}:q{index}:text_answer",
                target={"message_id": message_id, "chat_id": chat_id, "task_id": task_id},
                payload={
                    "card": answered_card(
                        questions[index],
                        index=index,
                        total=total,
                        task_id=task_id,
                        answer=answers[index],
                        is_last=nxt >= total,
                        icon_url=icon_url,
                        locale=ctx.locale,
                    )
                },
            )
    await interactions.patch_state(
        session,
        st.id,
        {
            "answers": answers,
            "current_index": nxt,
            "waiting_for_text": False,
            "waiting_since": None,
        },
    )
    if nxt < total:
        question = questions[nxt]
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=f"{st.id}:q{nxt}:brief",
            target={"chat_id": chat_id},
            payload={
                "markdown": question_brief(question, index=nxt, total=total, locale=ctx.locale)
            },
        )
        # brief 在前、卡片在后：卡片选项被截到 11 个字，用户得先看到完整说明再点。
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=f"{st.id}:q{nxt}:card",
            target={"chat_id": chat_id},
            payload={
                "card": choice_card(
                    question,
                    index=nxt,
                    total=total,
                    task_id=question_task_id(st.task_id_prefix or "", nxt),
                    icon_url=icon_url,
                    locale=ctx.locale,
                )
            },
        )
        ctx.log.info("choice_next_question", state_id=str(st.id), index=nxt)
        return
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"{st.id}:generating",
        target={"chat_id": chat_id},
        payload={"markdown": msg("choice_generating", ctx.locale)},
    )
    # 提交另起一个任务：这一轮（文本答案 / 卡片回调）必须立刻回用户，而把答案送回模型又要
    # 重开一条 SSE、跑上几分钟。带上触发它的入站事件，提交轮据此 load_inbound 拿回复上下文。
    await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="choice_submit",
            lane="normal",
            payload={
                "state_id": str(st.id),
                "bot_key": bot.bot_key,
                "platform_user_id": platform_user_id,
                "chat_type": chat_type,
                "chat_id": chat_id,
            },
            session_key=chat_id,
            inbound_event_id=ctx.task.inbound_event_id,
            dedupe_key=f"choice_submit:{st.id}",
        ),
    )
    ctx.log.info("choice_submit_enqueued", state_id=str(st.id), answers=len(answers))
