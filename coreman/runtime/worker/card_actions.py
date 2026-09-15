"""card_action 任务处理器：企微模板卡片事件的语义处理（投票卡片与限流切换卡片）。

所有应答都写 outbox(card_update)——网关 5 秒内以事件的 req_id 更新卡片；错过窗口的应答
被企微丢弃，用户再点一次会带来新的 req_id，届时状态已变（已答/已提交/已过期），照样应答。

任务终态一律 succeeded：卡片点击没有「失败」这回事，走到哪条分支只是语义不同，重试也换
不来别的结果（req_id 已经过期），所以把分支名写进 `result`，排查时一眼看出用户点了什么。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import interactions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, InboundEvent, InteractionState, OutboxItem
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import (
    answered_card,
    expired_card,
    notice_card,
    parse_task_id,
    waiting_card,
)
from coreman.runtime.worker.choice_flow import advance_choice, record_answer
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound

_DIGITS = re.compile(r"[0-9]+")


def _still_open(st: InteractionState, now: datetime) -> bool:
    """这条状态还等着用户点吗。不能只看 status：置 expired 的看护任务未必已经跑过。"""
    return st.status == "open" and (st.expires_at is None or st.expires_at > now)


def _selected(action: dict[str, Any], question_key: str) -> list[str]:
    """回调里勾中的选项 id。企微按 question_key 分组，不是这张卡的那一组一律当没勾。"""
    selected = action.get("selected")
    if not isinstance(selected, dict):
        return []
    return [str(v) for v in (selected.get(question_key) or [])]


class CardActionHandler:
    """模板卡片回调（快车道）：按 task_id 的 kind 分到投票卡片与限流切换卡片两条分支。"""

    kind = "card_action"

    async def run(self, ctx: TaskContext) -> None:
        action: dict[str, Any] = dict(ctx.task.payload.get("card_action") or {})
        async with ctx.session_factory() as session:
            bot = await session.get(Bot, ctx.task.bot_id)
            if bot is None or not bot.enabled:
                ctx.log.warning("bot_disabled")
                await tasks.finish(
                    session, ctx.task.id, status="cancelled", error_code="bot_disabled"
                )
                await session.commit()
                return
            inbound = await load_inbound(session, ctx.task)
            icon = str(await ctx.settings_store.get("card_icon_url", default="") or "")
            task_id = str(action.get("task_id") or "")
            if bot.platform == "feishu":
                sent = await session.scalar(
                    select(OutboxItem.id).where(
                        OutboxItem.bot_id == bot.id,
                        OutboxItem.status == "sent",
                        OutboxItem.payload["_feishu_message_id"].astext
                        == str(inbound.reply_context.get("message_id") or ""),
                        OutboxItem.payload["card"]["task_id"].astext == task_id,
                        OutboxItem.target["chat_id"].astext == inbound.chat_id,
                    )
                )
                if sent is None:
                    await tasks.finish(
                        session, ctx.task.id, status="succeeded", result={"card": "ignored"}
                    )
                    await session.commit()
                    return
            parsed = parse_task_id(task_id)
            if parsed is None:
                # 不是我们发的卡（别的应用、或者编码换过代）：不猜，也不回卡片。
                ctx.log.warning("card_task_id_unknown", card_task_id=task_id)
                result = "unknown"
            else:
                kind, prefix, index = parsed
                # 发起人比对与状态作用域都按解析后的明文 userid：提问那一轮就是这么落的
                # scope（见 chat_handler._open_choice）。直接拿载荷里的密文 open_userid 去
                # 比，这个人会被判成「别人点的卡」，自己的卡永远点不动。
                speaker = await resolve_speaker(
                    session,
                    platform=bot.platform,
                    platform_user_id=str(ctx.task.payload.get("platform_user_id") or ""),
                    resolver=ctx.openuserid,
                )
                st = await interactions.find_by_prefix(
                    session, bot_id=bot.id, task_id_prefix=prefix
                )
                if kind == "choice":
                    result = await self._choice(
                        session,
                        ctx,
                        bot,
                        inbound,
                        st,
                        action,
                        task_id=task_id,
                        index=index,
                        speaker=speaker.platform_user_id,
                        icon=icon,
                    )
                else:
                    result = await self._ratelimit(
                        session,
                        ctx,
                        bot,
                        inbound,
                        st,
                        action,
                        task_id=task_id,
                        prefix=prefix,
                        speaker=speaker.platform_user_id,
                        icon=icon,
                    )
            await tasks.finish(session, ctx.task.id, status="succeeded", result={"card": result})
            await session.commit()
        ctx.log.info("card_action_done", result=result)

    @staticmethod
    async def _reply_card(
        session: AsyncSession,
        bot: Bot,
        inbound: InboundEvent,
        task_id: str,
        card: dict[str, Any],
    ) -> None:
        """卡片应答只入出站箱：网关拿着这条的 req_id 调 `aibot_respond_update_msg`。

        幂等键按入站事件定：同一次点击被重试几遍都只更新一次卡片；用户再点一次是另一个
        事件、另一个 req_id，那一条照样发得出去。
        """
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="card_update",
            dedupe_key=f"{inbound.id}:card_update",
            target={
                "req_id": str((inbound.reply_context or {}).get("req_id") or ""),
                "task_id": task_id,
                **(
                    {
                        "message_id": inbound.reply_context.get("message_id"),
                        "chat_id": inbound.chat_id,
                    }
                    if bot.platform == "feishu"
                    else {}
                ),
            },
            payload={"card": card},
        )

    @staticmethod
    def _answer_text(question: dict[str, Any], ids: list[str], locale: str) -> str:
        """勾中的 `opt_N` 还原成选项原文：卡片上的文字被截到 11 个字，回执要写完整的。

        顺序按用户勾选的顺序，不重排——多选时这就是他心里的主次。
        """
        options = [o for o in (question.get("options") or []) if isinstance(o, dict)]
        labels: list[str] = []
        for opt in ids:
            idx = opt[4:]
            # 与 cards.parse_task_id 同理：`isdigit()` 放行的「②」会让下一步的 int() 抛错。
            if opt.startswith("opt_") and _DIGITS.fullmatch(idx) and int(idx) < len(options):
                labels.append(str(options[int(idx)].get("label") or opt))
        return ", ".join(labels) if labels else msg("card_no_selection", locale)

    async def _choice(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        bot: Bot,
        inbound: InboundEvent,
        st: InteractionState | None,
        action: dict[str, Any],
        *,
        task_id: str,
        index: int,
        speaker: str,
        icon: str,
    ) -> str:
        """AskUserQuestion 的投票卡片：记下答案，再把这一轮推到下一题或提交。"""
        if st is None or not _still_open(st, datetime.now(UTC)):
            # 状态没了（答完删了、reset 清了、看护置过期）：把卡换成过期卡，按钮就此失效。
            await self._reply_card(
                session,
                bot,
                inbound,
                task_id,
                expired_card(task_id, icon_url=icon, locale=ctx.locale),
            )
            return "expired"
        state = dict(st.state)
        context = dict(state.get("context") or {})
        if str(context.get("platform_user_id") or "") != speaker:
            # 群里别人点了提问人的卡：静默——回一张卡就等于替提问人把他的卡改掉了。
            ctx.log.info("card_not_initiator", state_id=str(st.id), speaker=speaker)
            return "ignored"
        current = int(state.get("current_index") or 0)
        questions = [q for q in (state.get("questions") or []) if isinstance(q, dict)]
        if index != current or index >= len(questions):
            # 往上翻出来的旧卡片：那一题早答过了，再写一次会把后面的答案挤错位。
            ctx.log.info("card_stale_index", state_id=str(st.id), index=index, current=current)
            return "ignored"
        question = questions[index]
        ids = _selected(action, "choice_answer")
        if "opt_other" in ids:
            # 「其他」不是一个答案，是「我要打字」：卡片换成占位卡，答案等下一条消息。
            await interactions.patch_state(
                session,
                st.id,
                {"waiting_for_text": True, "waiting_since": datetime.now(UTC).isoformat()},
            )
            await self._reply_card(
                session,
                bot,
                inbound,
                task_id,
                waiting_card(question, task_id=task_id, icon_url=icon, locale=ctx.locale),
            )
            ctx.log.info("choice_waiting_text", state_id=str(st.id), index=index)
            return "waiting"
        total = len(questions)
        answer = self._answer_text(question, ids, ctx.locale)
        answers = record_answer(state, index, answer)
        # 先回卡片再推进：推进要写状态、排提交任务，都比这张卡耐等——而卡片只有 5 秒窗口。
        await self._reply_card(
            session,
            bot,
            inbound,
            task_id,
            answered_card(
                question,
                index=index,
                total=total,
                task_id=task_id,
                answer=answer,
                is_last=index + 1 >= total,
                icon_url=icon,
                locale=ctx.locale,
            ),
        )
        await advance_choice(
            session,
            ctx=ctx,
            bot=bot,
            st=st,
            questions=questions,
            answers=answers,
            index=index,
            chat_id=str(context.get("chat_id") or ctx.task.payload.get("chat_id") or speaker),
            chat_type=str(
                context.get("chat_type") or ctx.task.payload.get("chat_type") or "single"
            ),
            platform_user_id=speaker,
            icon_url=icon,
        )
        return "answered"

    async def _ratelimit(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        bot: Bot,
        inbound: InboundEvent,
        st: InteractionState | None,
        action: dict[str, Any],
        *,
        task_id: str,
        prefix: str,
        speaker: str,
        icon: str,
    ) -> str:
        """限流切换卡片：继续等，还是排一个切换任务。"""

        async def notice(title_key: str, desc_key: str, **kw: object) -> None:
            await self._reply_card(
                session,
                bot,
                inbound,
                task_id,
                notice_card(
                    task_id,
                    title=msg(title_key, ctx.locale),
                    desc=msg(desc_key, ctx.locale, **kw),
                    icon_url=icon,
                    locale=ctx.locale,
                ),
            )

        if st is not None and str(st.state.get("platform_user_id") or "") != speaker:
            ctx.log.info("card_not_initiator", state_id=str(st.id), speaker=speaker)
            return "ignored"
        if st is None or not _still_open(st, datetime.now(UTC)):
            # 这张卡不作数了。若这个人手里已经有更新的一张，指过去；否则就是过期。
            newer = await interactions.get_open(
                session,
                kind=interactions.KIND_RELAY_SWITCH,
                scope_key=interactions.choice_scope(bot.id, speaker),
            )
            if newer is not None and newer.task_id_prefix != prefix:
                await notice("rl_replaced_title", "rl_replaced_desc")
                return "replaced"
            await notice("rl_expired_title", "rl_expired_desc")
            return "expired"
        ids = _selected(action, "ratelimit_switch_choice")
        if not ids:
            # 企微允许不勾就提交；卡片还开着，提示一句让他再点一次。
            await notice("rl_no_option_title", "rl_no_option_desc")
            return "no_option"
        state = dict(st.state)
        current_name = str(state.get("current_name") or "")
        choice = ids[0]
        if choice == "opt_wait":
            if not await interactions.set_status(session, st.id, "submitted"):
                # 与并发的 opt_switch 撞上且抢输了：那次已经排了切换任务，这里再回一张
                # 「已选择继续等待」，用户会以为没切、实际正在切。与 opt_switch 分支同构。
                await notice("rl_expired_title", "rl_expired_desc")
                return "expired"
            await notice("rl_wait_title", "rl_wait_desc", current=current_name)
            ctx.log.info("relay_switch_declined", state_id=str(st.id))
            return "wait"
        if choice != "opt_switch":
            # 不认识的选项：状态留着开，别拿一次解析不了的点击把用户的卡作废掉。
            ctx.log.warning("card_option_unknown", state_id=str(st.id), option=choice)
            await notice("rl_unknown_title", "rl_unknown_desc", option=choice)
            return "unknown_option"
        if not await interactions.set_status(session, st.id, "submitted"):
            # 抢在前面的那一次点击已经把卡收了：绝不能再排一个切换任务。
            await notice("rl_expired_title", "rl_expired_desc")
            return "expired"
        await notice(
            "rl_switching_title",
            "rl_switching_desc",
            current=current_name,
            target=str(state.get("target_name") or ""),
        )
        # 真正的切换（改绑、重开会话、回执）另起一个任务：这一轮必须在 5 秒内把卡片回掉。
        await tasks.enqueue(
            session,
            NewTask(
                bot_id=bot.id,
                kind="relay_switch",
                lane="fast",
                payload={
                    "state_id": str(st.id),
                    "bot_key": bot.bot_key,
                    "platform_user_id": speaker,
                    "user_id": state.get("user_id"),
                    "chat_id": state.get("chat_id"),
                    "chat_type": state.get("chat_type"),
                    "target_relay_server_id": state.get("target_relay_id"),
                    "current_name": current_name,
                    "target_name": state.get("target_name"),
                },
                session_key=str(state.get("chat_id") or speaker),
                inbound_event_id=inbound.id,
                dedupe_key=f"relay_switch:{st.id}",
            ),
        )
        ctx.log.info("relay_switch_enqueued", state_id=str(st.id))
        return "switch"
