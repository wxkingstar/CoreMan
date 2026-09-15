"""会话列表与序号切换：都不进 AI、不落 chat_logs。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import interactions, sessions
from coreman.core.chat.session_switch import (
    PENDING_SECONDS,
    format_list,
    is_sessions_command,
    parse_choice,
    recent_sessions,
)
from coreman.core.i18n.messages import msg
from coreman.core.relay.models import backend_of
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import reply_once


class SessionSwitchStage(ChatStageBase):
    """sessions 列表与 5 分钟内的序号切换。"""

    async def _sessions(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
        """`sessions` 列表与 5 分钟内的序号切换；都不进 AI、不落 chat_logs。

        作用域是会话而不是发言者：群里切的是「这个群用哪条 relay 会话」，一个人切完全群
        跟着走，各人各一份反而会让同一个群分裂成几条上下文。

        排在待答文本之后：正等着用户打字回答问题时，「2」多半是在选第二个选项，不该被
        这里劫走当成会话序号。没有待选列表时数字就是普通消息，照常进 AI。
        """
        scope = interactions.session_scope(intake.bot.id, intake.session_key)
        now = datetime.now(UTC)
        if is_sessions_command(intake.text):
            items = await recent_sessions(
                session,
                bot_id=intake.bot.id,
                session_key=intake.session_key,
                relay_server_id=intake.bot.relay_server_id,
                locale=ctx.locale,
            )
            if not items:
                # 一条历史都没有就别开待答状态：接下来用户随口说的数字会被吞成选项。
                text = msg("no_sessions", ctx.locale)
            else:
                await interactions.open_state(
                    session,
                    bot_id=intake.bot.id,
                    kind=interactions.KIND_SESSION_SWITCH,
                    scope_key=scope,
                    state={
                        "sessions": [
                            {"relay_session_id": str(i.relay_session_id), "preview": i.preview}
                            for i in items
                        ]
                    },
                    expires_at=now + timedelta(seconds=PENDING_SECONDS),
                )
                text = format_list(items, now, ctx.locale)
            ctx.log.info("sessions_listed", count=len(items))
            await reply_once(session, ctx, reply_context=intake.inbound.reply_context, text=text)
            await tasks.finish(
                session, ctx.task.id, status="succeeded", result={"sessions": len(items)}
            )
            return True
        st = await interactions.get_open(
            session, kind=interactions.KIND_SESSION_SWITCH, scope_key=scope, now=now
        )
        if st is None:
            return False
        options = list(st.state.get("sessions") or [])
        index = parse_choice(intake.text, len(options))
        if index is None:
            return False
        chosen = options[index - 1]
        await sessions.switch_to(
            session,
            bot_id=intake.bot.id,
            session_key=intake.session_key,
            relay_session_id=uuid.UUID(str(chosen["relay_session_id"])),
            backend=backend_of(
                intake.bot.model, intake.relay.model_provider if intake.relay else None
            ),
            speaker_user_id=intake.speaker.user_id,
        )
        await interactions.set_status(session, st.id, "submitted")
        await reply_once(
            session,
            ctx,
            reply_context=intake.inbound.reply_context,
            text=msg(
                "session_switched",
                ctx.locale,
                index=index,
                preview=str(chosen.get("preview") or ""),
            ),
        )
        await tasks.finish(
            session, ctx.task.id, status="succeeded", result={"session_switch": index}
        )
        ctx.log.info("session_switched", index=index)
        return True
