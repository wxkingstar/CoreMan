"""入站阶段（spec §8.2 步骤 1–5）：加载机器人、入站消息与发言者。

公告、白名单、内置命令、待答文本答案都在这里就地了结；relay 不可用排在最后。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import interactions
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.commands import classify_command, is_cancel_word
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    RelayServer,
)
from coreman.core.i18n.messages import msg
from coreman.core.timeutils import parse_ts
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.inbound import (
    joined_text,
    message_type_of,
    strip_mention,
)
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.chat.records import log_entry
from coreman.runtime.worker.choice_flow import (
    FREE_TEXT_WAIT_SECONDS,
    advance_choice,
    record_answer,
)
from coreman.runtime.worker.commands import do_reset, do_stop
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound, reply_once


class IntakeStage(ChatStageBase):
    """入站：能就地了结的分支在这里全部了结。"""

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        """这一轮从哪儿来。chat 是入站消息；子类可以换成别的入口（提交轮读状态快照）。"""
        if ctx.task.payload.get("collaboration_id"):
            from coreman.runtime.worker.chat.collaboration import resolve

            return await resolve(session, ctx)
        return await self._intake(session, ctx)

    def _needs_content(self) -> bool:
        """要不要走「组装内容（下载媒体）」这一步。入口自带成品文本的子类返回 False。"""
        return True

    async def _intake(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        """加载 bot / 入站消息 / relay 与发言者；能就地了结的分支在这里全部了结。"""
        bot = await session.get(Bot, ctx.task.bot_id)
        if bot is None or not bot.enabled:
            ctx.log.warning("bot_disabled")
            await tasks.finish(session, ctx.task.id, status="cancelled", error_code="bot_disabled")
            return None
        inbound = await load_inbound(session, ctx.task)
        message: dict[str, Any] = ctx.task.payload.get("message") or inbound.payload
        parts = [p for p in (message.get("parts") or []) if isinstance(p, dict)]
        chat_id = str(message.get("chat_id") or inbound.chat_id)
        chat_type = str(message.get("chat_type") or inbound.chat_type)
        session_key = ctx.task.session_key or chat_id
        sender = message.get("sender") or {}
        if sender.get("sender_type", "user") != "user":
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="bot_requires_collaboration"
            )
            return None
        platform_user_id = str(
            sender.get("platform_user_id") or inbound.sender_platform_user_id or ""
        )
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=platform_user_id,
            resolver=ctx.openuserid,
        )
        text = strip_mention(joined_text(parts), bot.name)
        if bot.platform == "feishu":
            from coreman.runtime.worker.feishu_escalations import consume_reply

            if await consume_reply(session, ctx, bot, inbound, speaker, parts, text):
                return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            chat_id,
            chat_type,
            session_key,
            text,
            message_type_of(parts),
        )
        if await self._announced(session, ctx, intake):
            return None
        if await self._denied(session, ctx, intake):
            return None
        if await self._command(session, ctx, intake):
            return None
        if await self._pending_answer(session, ctx, intake):
            return None
        if await self._sessions(session, ctx, intake):
            return None
        if relay is None or not relay.is_active:
            ctx.log.warning("relay_unavailable", relay_id=str(bot.relay_server_id))
            await reply_once(
                session,
                ctx,
                reply_context=inbound.reply_context,
                text=msg("relay_error", ctx.locale, relay=msg("relay_unconfigured", ctx.locale)),
            )
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            ctx.chat_logs.submit(
                log_entry(
                    ctx,
                    intake,
                    status="error",
                    error_code="relay_unavailable",
                    error_message="机器人未绑定可用的运行时",
                )
            )
            return None
        return intake, relay, parts

    async def _announced(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
        """公告命中：直接以公告回复并结束，不进 AI、不落 chat_logs、不动会话与状态。"""
        hit = await find_announcement(
            session, bot_id=intake.bot.id, relay_server_id=intake.bot.relay_server_id
        )
        if hit is None:
            return False
        ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
        await reply_once(session, ctx, reply_context=intake.inbound.reply_context, text=hit.content)
        await tasks.finish(session, ctx.task.id, status="succeeded", result={"announcement": True})
        return True

    async def _denied(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
        """白名单非空且发言者不在其中（含身份未知）→ 固定拒绝文案，不落 chat_logs。"""
        stmt = select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == intake.bot.id)
        allowed = set((await session.execute(stmt)).scalars())
        if not allowed or (
            intake.speaker.user_id is not None and intake.speaker.user_id in allowed
        ):
            return False
        ctx.log.info("denied_not_allowed", platform_user_id=intake.speaker.platform_user_id)
        await reply_once(
            session,
            ctx,
            reply_context=intake.inbound.reply_context,
            text=msg("no_permission", ctx.locale),
        )
        await tasks.finish(session, ctx.task.id, status="succeeded", result={"denied": True})
        return True

    async def _command(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
        """内置命令就地处理，不二次入队、不落 chat_logs（命令不是一轮对话）。"""
        command = classify_command(intake.text)
        if command is None:
            return False
        pid = intake.speaker.platform_user_id
        if command == "reset":
            text = await do_reset(session, ctx, intake.bot.id, intake.session_key, pid)
        elif command == "stop":
            text = await do_stop(session, ctx, intake.bot.id, intake.session_key, pid)
        else:
            text = msg("help", ctx.locale)
        ctx.log.info("builtin_command", command=command)
        await reply_once(session, ctx, reply_context=intake.inbound.reply_context, text=text)
        await tasks.finish(session, ctx.task.id, status="succeeded", result={"command": command})
        return True

    async def _pending_answer(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake
    ) -> bool:
        """AskUserQuestion 的自由文本答案 / 取消词（spec §8.2 步骤 5）。

        排在命令之后、relay 检查之前：答一道题既不进模型也不碰 relay，机器人此刻没有可用
        实例照样答得下去；而 reset/stop 得先把待答状态清掉，不能被这里吞成一句答案。

        作用域按 `intake.speaker.platform_user_id`（解密后的明文 userid）建，与提问那一轮
        写状态时用的是同一个 id——用原始载荷里的密文 id 会落在另一个 scope 上，这个人就
        永远答不上自己的题。
        """
        scope = interactions.choice_scope(intake.bot.id, intake.speaker.platform_user_id)
        st = await interactions.get_open(session, kind=interactions.KIND_CHOICE, scope_key=scope)
        if st is None:
            return False
        if is_cancel_word(intake.text):
            # 取消随时可用（不限于正在等文字）：卡片还挂在那儿，用户想退出就得退得掉。
            await interactions.remove(session, st.id)
            ctx.log.info("choice_cancelled", state_id=str(st.id))
            await reply_once(
                session,
                ctx,
                reply_context=intake.inbound.reply_context,
                text=msg("choice_cancelled", ctx.locale),
            )
            await tasks.finish(
                session, ctx.task.id, status="succeeded", result={"choice": "cancelled"}
            )
            return True
        state = dict(st.state)
        # 没点「其他」就不等文字：这时的消息是新话题，照常进 AI。纯图片（没有文字）同理，
        # 吞下去只会把一个空答案写进状态。
        if not state.get("waiting_for_text") or not intake.text.strip():
            return False
        since = parse_ts(state.get("waiting_since"))
        if since is None or (datetime.now(UTC) - since).total_seconds() > FREE_TEXT_WAIT_SECONDS:
            # 等了半小时还没打字，这条多半是新话题：清掉等待标记放行，状态与卡片仍留着。
            await interactions.patch_state(
                session, st.id, {"waiting_for_text": False, "waiting_since": None}
            )
            ctx.log.info("choice_wait_expired", state_id=str(st.id))
            return False
        questions = [q for q in (state.get("questions") or []) if isinstance(q, dict)]
        index = int(state.get("current_index") or 0)
        answers = record_answer(state, index, intake.text)
        await reply_once(
            session,
            ctx,
            reply_context=intake.inbound.reply_context,
            text=msg("answer_recorded", ctx.locale, answer=intake.text[:60]),
        )
        await advance_choice(
            session,
            ctx=ctx,
            bot=intake.bot,
            st=st,
            questions=questions,
            answers=answers,
            index=index,
            chat_id=intake.chat_id,
            chat_type=intake.chat_type,
            platform_user_id=intake.speaker.platform_user_id,
            icon_url=await self._icon_url(ctx),
        )
        await tasks.finish(session, ctx.task.id, status="succeeded", result={"choice": "answered"})
        return True
