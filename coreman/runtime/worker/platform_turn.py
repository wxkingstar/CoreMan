"""平台发起的一轮的处理器基类：发言者固定为所有者，消息是平台写好的文本，没有入站内容。

交回事项、子任务及其结果、下班编译、审批结果都是这样的一轮：流水线（会话、提示词、个人工具、
沙箱、转录、收尾）与所有者私聊的一轮完全相同，只有入站解析不同。子类给出任务类型与这一轮的
文本；带入站事件的任务（审批结果回合沿用提交那一轮的）照旧用它，不带的用一个不落库的替身。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns, runtime
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    RelayServer,
    Task,
    User,
    UserIdentity,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


def stand_in(bot: Bot, row: AssistantSettings, task: Task) -> InboundEvent:
    """没有入站事件的一轮用的替身：不加入会话、不落库，只给流水线提供会话坐标。

    回复上下文里只有所有者的私聊，没有被回复的消息：这一轮的回复作为一条新消息发过去。
    """
    return InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"platform-turn:{task.id}",
        kind="message",
        chat_type="single",
        chat_id=row.chat_id,
        sender_platform_user_id=row.platform_user_id,
        sender_open_id=row.open_id,
        payload={},
        reply_context={"chat_id": row.chat_id, "chat_type": "single"},
        received_at=utcnow(),
    )


class PlatformTurnHandler(ChatTaskHandler):
    """子类设置 `kind` 并实现 `_text`；其余与私聊一轮相同。"""

    kind = ""
    # 取消任务时写的错误代码：这一轮已经没有意义、所有者换了人或账号停用、运行时有公告。
    INACTIVE = "platform_turn_inactive"
    OWNER_CHANGED = "platform_owner_changed"
    ANNOUNCED = "platform_turn_announced"

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        """这一轮交给模型的文本；返回 None 表示这一轮已经没有意义，任务按 `INACTIVE` 取消。"""
        raise NotImplementedError

    async def _cancel(self, session: AsyncSession, ctx: TaskContext, code: str) -> None:
        await tasks.finish(session, ctx.task.id, status="cancelled", error_code=code)

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        if bot is None or not bot.enabled or not is_assistant(bot) or bot.owner_id is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        text = await self._text(session, ctx, bot)
        if text is None:
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform="feishu",
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, bot.owner_id, populate_existing=True)
        if speaker.user_id != bot.owner_id or user is None or user.status != "active":
            await self._cancel(session, ctx, self.OWNER_CHANGED)
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        hit = await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id)
        if hit is not None:
            ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
            await self._cancel(session, ctx, self.ANNOUNCED)
            return None
        if ctx.task.inbound_event_id is not None:
            inbound = await load_inbound(session, ctx.task)
        else:
            found = await self._stand_in(session, ctx, bot)
            if found is None:
                return None
            inbound = found
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            inbound.chat_id,
            inbound.chat_type,
            ctx.task.session_key or inbound.chat_id,
            text,
            self.kind,
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _stand_in(
        self, session: AsyncSession, ctx: TaskContext, bot: Bot
    ) -> InboundEvent | None:
        """没有入站事件的一轮的替身；这一轮不该跑时结掉任务并返回 None。"""
        row = await session.get(AssistantSettings, bot.id)
        if (
            row is None
            or not platform_turns.is_platform(ctx.task)
            or not platform_turns.owns_session(row, ctx.task.session_key)
        ):
            await self._cancel(session, ctx, self.INACTIVE)
            return None
        reason = await runtime.turn_blocker(session, bot)
        if reason is not None:
            # 没有人在等这一轮：暂停期间不为平台自己发起的任务再私信所有者，只结掉任务。
            ctx.log.warning("assistant_paused", reason=reason)
            await tasks.finish(session, ctx.task.id, status="failed", error_code="assistant_paused")
            return None
        return stand_in(bot, row, ctx.task)

    def _needs_content(self) -> bool:
        """消息是平台写好的成品文本，没有媒体要下载。"""
        return False
