"""credential_resume：用户提交凭证之后，在原会话里续接那一轮。

与普通 chat 的差别只有入口：读凭证请求而不是入站消息（入站事件沿用来源那一轮的），发言者
固定为发起人；企业微信的流从创建起就是主动推送（续接轮没有可用的 req_id）。凭证照常在开轮时
按发言者注入（chat/credentials.py），这一轮的消息里只有键名和用户的原始请求。其余全部复用
ChatTaskHandler。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.chat import sessions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    ChatSession,
    CredentialRequest,
    RelayServer,
    User,
    UserIdentity,
)
from coreman.core.personal_credentials import cards
from coreman.core.personal_credentials.service import RESUME_KIND, newer_turn_by_other
from coreman.runtime.worker.chat_handler import (
    ChatTaskHandler,
    Intake,
    Prepared,
    joined_text,
    strip_mention,
)
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound

SAVED_NOTICE = "凭证已保存，下次对话时生效。"
RESET_NOTICE = "凭证已保存。对话已重置，请重新发起刚才的请求。"
SUPERSEDED_NOTICE = "凭证已保存。对话里已有新的请求，请重新发起刚才的请求。"


class _SessionChanged(ValueError):
    """开轮时发现提问的那个对话已经不在了：不能在另一个会话里续接。"""


class CredentialResumeHandler(ChatTaskHandler):
    kind = RESUME_KIND

    async def run(self, ctx: TaskContext) -> None:
        try:
            await super().run(ctx)
        except _SessionChanged:
            # 开轮事务已随异常回滚（没有流、没有记录、没有新会话），这里另开事务收尾：
            # 与 `_resolve` 里同一情形一样，告诉用户凭证已保存，任务按取消结掉。
            async with ctx.session_factory() as session:
                bot = await session.get(Bot, ctx.task.bot_id)
                raw_id = ctx.task.payload.get("credential_request_id")
                row = await session.get(CredentialRequest, uuid.UUID(str(raw_id)))
                if bot is not None and row is not None:
                    await self._saved_notice(session, bot, row, RESET_NOTICE)
                await tasks.finish(
                    session,
                    ctx.task.id,
                    status="cancelled",
                    error_code="credential_resume_session_changed",
                    only_active=True,
                )
                await session.commit()

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        bot = await session.get(Bot, ctx.task.bot_id)
        raw_id = ctx.task.payload.get("credential_request_id")
        row = (
            await session.get(
                CredentialRequest,
                uuid.UUID(str(raw_id)),
                with_for_update=True,
                populate_existing=True,
            )
            if raw_id
            else None
        )
        if (
            bot is None
            or row is None
            or row.status != "submitted"
            or row.resume_task_id != ctx.task.id
        ):
            # 请求已不再指向这个任务（或机器人没了）：这条续接过时了，没有要告诉用户的。
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_resume_inactive"
            )
            return None
        if not bot.enabled:
            # 停用的机器人网关没在跑，这条通知要等重新启用才会发出，总好过什么都不说。
            await self._saved_notice(session, bot, row)
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_resume_inactive"
            )
            return None
        identity = await session.scalar(
            select(UserIdentity)
            .where(UserIdentity.user_id == row.user_id, UserIdentity.platform == bot.platform)
            .limit(1)
        )
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=identity.platform_user_id if identity else "",
            resolver=ctx.openuserid,
        )
        user = await session.get(User, row.user_id, populate_existing=True)
        allowed = set(
            await session.scalars(
                select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id)
            )
        )
        if (
            speaker.user_id != row.user_id
            or user is None
            or user.status != "active"
            or (allowed and row.user_id not in allowed)
        ):
            await self._saved_notice(session, bot, row)
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_owner_changed"
            )
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await self._saved_notice(session, bot, row)
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        session_key = row.origin_session_key or row.origin_chat_id
        current = await session.get(ChatSession, (bot.id, session_key), populate_existing=True)
        if current is None or current.relay_session_id != row.origin_relay_session_id:
            # 提问之后对话被重置或切走：续接会落进另一个对话，上下文对不上，只保存、请用户重新发起。
            await self._saved_notice(session, bot, row, RESET_NOTICE)
            await tasks.finish(
                session,
                ctx.task.id,
                status="cancelled",
                error_code="credential_resume_session_changed",
            )
            return None
        if await newer_turn_by_other(session, row):
            # 提交之后、续接任务运行之前，会话里来了别人的新请求：续接会接到它上面，
            # 还带着发起人的凭证。只保存、请用户重新发起。
            await self._saved_notice(session, bot, row, SUPERSEDED_NOTICE)
            await tasks.finish(
                session,
                ctx.task.id,
                status="cancelled",
                error_code="credential_resume_superseded",
            )
            return None
        inbound = await load_inbound(session, ctx.task)
        # 引用来源那一轮的原始请求：会话里可能又来过别的消息，续接要接回它。
        parts = [p for p in (inbound.payload.get("parts") or []) if isinstance(p, dict)]
        original = strip_mention(joined_text(parts), bot.name)
        # 与 store.save 返回的键名同序（已排序），保存通知和续接消息列出的顺序一致。
        text = cards.resume_text(sorted(str(field["key"]) for field in row.fields), original)
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            row.origin_chat_id,
            row.origin_chat_type,
            session_key,
            text,
            RESUME_KIND,
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        """开轮时（已持 bot 行锁）再确认一次：续接只进提问的那个 relay 会话，绝不另起新的。

        `_resolve` 的检查与开轮之间隔着排队等待，期间换模型、换运行时、清会话都会让默认的
        get_or_create 悄悄建一个空会话，续接消息就在没有来源上下文的会话里跑了。
        """
        raw_id = ctx.task.payload.get("credential_request_id")
        row = await session.get(CredentialRequest, uuid.UUID(str(raw_id)))
        info = await super()._session_info(session, ctx, intake, backend)
        if row is None or info.relay_session_id != row.origin_relay_session_id:
            # get_or_create 在会话缺失、后端变了或已过期时会给出新的 id；异常让开轮事务回滚，
            # 它写进去的新映射不会留下。
            raise _SessionChanged("credential resume session changed before dispatch")
        return info

    @staticmethod
    async def _saved_notice(
        session: AsyncSession, bot: Bot, row: CredentialRequest, text: str = SAVED_NOTICE
    ) -> None:
        """续不下去时只告诉用户凭证已保存：提交时已经承诺过会继续，不能悄悄收场。"""
        if not row.delivery_chat_id:
            return
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=f"credential-request:{row.id}:resume-skipped",
            target={"chat_id": row.delivery_chat_id},
            payload={"markdown": text},
        )

    def _needs_content(self) -> bool:
        """续接消息是系统写好的成品文本，没有媒体要下载。"""
        return False

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """企业微信：没有可用的 req_id，流从创建就走 outbox 主动推送；飞书沿用来源的回复上下文。"""
        if intake.bot.platform != "wecom":
            return {}
        now = datetime.now(UTC).isoformat()
        return {
            "delivery_mode": "proactive",
            "background_state": {
                "mode": "proactive",
                "offset": 0,
                "finish_suffix": "",
                "switched_at": now,
            },
            "reply_context": {
                "gateway_instance": ctx.instance_id,
                "received_at": now,
                "chat_type": intake.chat_type,
                "chat_id": intake.chat_id,
            },
        }

    def _after_supervisor(self, pre: Prepared) -> None:
        if pre.intake.bot.platform == "wecom":
            pre.supervisor.start_proactive(pre.started_clock)
