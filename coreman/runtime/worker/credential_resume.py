"""credential_resume：用户提交凭证之后，在原会话里续接那一轮。

与普通 chat 的差别只有入口：读凭证请求而不是入站消息（入站事件沿用来源那一轮的），发言者
固定为发起人；企业微信的流从创建起就是主动推送（续接轮没有可用的 req_id）。凭证照常在开轮时
按发言者注入（chat/credentials.py），这一轮的消息里只有键名。其余全部复用 ChatTaskHandler。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    CredentialRequest,
    RelayServer,
    User,
    UserIdentity,
)
from coreman.core.personal_credentials import cards
from coreman.core.personal_credentials.service import RESUME_KIND
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake, Prepared
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound


class CredentialResumeHandler(ChatTaskHandler):
    kind = RESUME_KIND

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
            or not bot.enabled
            or row is None
            or row.status != "submitted"
            or row.resume_task_id != ctx.task.id
        ):
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
            if row.delivery_chat_id:
                await outbox.add(
                    session,
                    bot_id=bot.id,
                    platform=bot.platform,
                    kind="send",
                    dedupe_key=f"credential-request:{row.id}:resume-skipped",
                    target={"chat_id": row.delivery_chat_id},
                    payload={"markdown": "凭证已保存，下次对话时生效。"},
                )
            await tasks.finish(
                session, ctx.task.id, status="cancelled", error_code="credential_owner_changed"
            )
            return None
        relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
        if relay is None or not relay.is_active:
            await tasks.finish(
                session, ctx.task.id, status="failed", error_code="relay_unavailable"
            )
            return None
        inbound = await load_inbound(session, ctx.task)
        # 与 store.save 返回的键名同序（已排序），保存通知和续接消息列出的顺序一致。
        text = cards.resume_text(sorted(str(field["key"]) for field in row.fields))
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            row.origin_chat_id,
            row.origin_chat_type,
            row.origin_session_key or row.origin_chat_id,
            text,
            RESUME_KIND,
        )
        return intake, relay, [{"type": "text", "text": text}]

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
