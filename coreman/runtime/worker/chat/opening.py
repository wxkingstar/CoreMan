"""开流阶段：等被替代的任务退出，一次建好会话、提示词、env、请求体与 task_streams。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import build_system_access
from coreman.core.bus import tasks
from coreman.core.chat import sessions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    Bot,
    RelayServer,
)
from coreman.core.i18n.messages import msg
from coreman.core.knowledge.installation import effective_env
from coreman.core.prompting import (
    Speaker,
    build_env,
    build_system_prompt,
    env_keys_for_log,
    load_segments,
    sanitize_user_input,
)
from coreman.core.relay.client import ChatRequest
from coreman.core.relay.models import backend_of
from coreman.runtime.worker.background import TimeoutSupervisor
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Intake, Prepared
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import open_stream
from coreman.runtime.worker.stream_writer import StreamWriter


def session_viewer_url(ctx: TaskContext, relay: RelayServer, relay_session_id: uuid.UUID) -> str:
    """会话查看链接：运行时一律由节点提供，查看器走管理台对该节点的反向代理。"""
    return (
        f"{ctx.public_base_url}/api/admin/runtime-nodes/{relay.runtime_node_id}"
        f"/{relay.model_provider}/session/{relay_session_id}"
    )


class OpenStage(ChatStageBase):
    """开流：同会话串行、会话与提示词、task_streams 与超时看护。"""

    async def _wait_superseded(self, ctx: TaskContext, victims: list[int]) -> None:
        """等被替代的任务真的退出，免得两轮同时往同一个 relay 会话里写。最多等 5 秒。"""
        if not victims:
            return
        for _ in range(self.SUPERSEDE_POLLS):
            async with ctx.session_factory() as session:
                rows = [await tasks.get(session, tid) for tid in victims]
            if all(row is None or row.status not in tasks.ACTIVE for row in rows):
                return
            await asyncio.sleep(self.SUPERSEDE_POLL_SECONDS)
        ctx.log.warning("superseded_wait_timeout", victims=victims)
        raise TimeoutError("session_busy")

    async def _open(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        intake: Intake,
        relay: RelayServer,
        started: float,
    ) -> Prepared:
        """会话、提示词、env、请求体与 task_streams 一次建好。"""
        # 换机事务持有 bot 行锁；等待提交后从当前实例构造请求，避免派发到旧实例。
        bot = await session.scalar(select(Bot).where(Bot.id == intake.bot.id).with_for_update())
        current_relay = (
            await session.get(RelayServer, bot.relay_server_id)
            if bot and bot.relay_server_id
            else None
        )
        if bot is None or not bot.enabled or current_relay is None or not current_relay.is_active:
            raise ValueError("bot_or_relay_changed_before_dispatch")
        current_task = await tasks.get(session, ctx.task.id)
        if (
            current_task is None
            or current_task.cancel_requested_at
            or current_task.status not in tasks.ACTIVE
        ):
            raise ValueError("task cancelled before dispatch")
        if ctx.task.payload.get("collaboration_id"):
            from coreman.core.db.models import BotCollaboration

            collaboration = await session.get(
                BotCollaboration,
                uuid.UUID(ctx.task.payload["collaboration_id"]),
                populate_existing=True,
            )
            phase = ctx.task.payload.get("collaboration_phase")
            if (
                collaboration is None
                or collaboration.expires_at <= datetime.now(UTC)
                or collaboration.status != ("helper_running" if phase == "helper" else "resuming")
            ):
                raise ValueError("collaboration cancelled before dispatch")
        relay = current_relay
        intake = replace(intake, bot=bot, relay=relay)
        if intake.speaker.known:
            fresh_speaker = await resolve_speaker(
                session, platform=bot.platform, platform_user_id=intake.speaker.platform_user_id
            )
            if fresh_speaker.user_id != intake.speaker.user_id:
                fresh_speaker = Speaker(intake.speaker.platform_user_id, None, None, None)
            intake = replace(intake, speaker=fresh_speaker)
        writer = StreamWriter(ctx)
        queued = self._queued_seconds(ctx)
        if queued is not None:
            writer.set_thinking_line(msg("queued_notice", ctx.locale, seconds=queued))
        backend = backend_of(bot.model, relay.model_provider)
        info = await self._session_info(session, ctx, intake, backend)
        session_url = session_viewer_url(ctx, relay, info.relay_session_id)
        access = await build_system_access(
            session,
            ctx.cipher,
            bot=bot,
            speaker=intake.speaker,
            issuer=str(await ctx.settings_store.get("jwt_issuer", default="coreman")),
        )
        system_prompt = await self._system_prompt(ctx, intake, info, backend, access.prompt)
        env = build_env(
            bot_key=bot.bot_key,
            platform=bot.platform,
            chat_id=intake.chat_id,
            chat_type=intake.chat_type,
            platform_user_id=intake.speaker.platform_user_id,
            session_id=str(info.relay_session_id),
            speaker=intake.speaker,
            bot_env=await effective_env(session, ctx.cipher, bot),
        )
        env.update(access.env)
        from coreman.runtime.worker.chat.collaboration import configure

        system_prompt, env = await configure(session, ctx, intake, info, system_prompt, env)
        # 只记键名：env 的值里混着机器人配的密钥，一个都不能进日志流。
        ctx.log.info("request_built", backend=backend, env_keys=env_keys_for_log(env))
        request = ChatRequest(
            model=bot.model,
            system_prompt=system_prompt,
            user_content=sanitize_user_input(intake.text),
            working_dir=bot.working_dir,
            session_id=str(info.relay_session_id),
            backend=backend,
            effort=bot.effort_level,
            verbosity_level=bot.verbosity_level,
            env_vars=env,
        )
        stream_kwargs: dict[str, Any] = {
            "reply_context": intake.inbound.reply_context,
            "session_url": session_url,
            "platform": bot.platform,
        }
        stream_kwargs.update(self._stream_kwargs(ctx, intake))
        await open_stream(session, ctx, **stream_kwargs)
        if info.is_new and ctx.task.payload.get("collaboration_phase") != "helper":
            writer.add_text(msg("session_link_prefix", ctx.locale, url=session_url))
        writer.set_thinking_line(msg("thinking_start", ctx.locale))
        # IM 投递策略固定在底层，不读取旧的员工/全局 agent_timeout_seconds。
        # 企微在流窗口结束前交接；飞书由网关关闭打字机效果后继续更新同一卡片，
        # 不因单轮运行时长切成主动消息。硬 TTL 仍从任务认领时刻起算。
        agent_timeout = self.timing.wecom_background_after if bot.platform == "wecom" else None
        supervisor = TimeoutSupervisor(
            ctx,
            writer,
            agent_timeout=agent_timeout,
            timing=self.timing,
            verbosity_level=bot.verbosity_level,
            session_url=session_url,
            chat_id=intake.chat_id,
            platform=bot.platform,
            started_at=started,
        )
        pre = Prepared(intake, relay, info, request, writer, session_url, started, supervisor)
        self._after_supervisor(pre)
        return pre

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        """本轮用哪个 relay 会话。默认按 session_key 取（或新建）并刷新 chat_sessions。"""
        if ctx.task.payload.get("collaboration_phase") == "resume":
            from coreman.core.db.models import BotCollaboration

            row = await session.get(
                BotCollaboration, uuid.UUID(ctx.task.payload["collaboration_id"])
            )
            if row is None:
                raise ValueError("collaboration missing")
            # _open holds the bot lock and has refreshed intake.relay.
            # The earlier resolve transaction cannot fence an intervening runtime switch.
            if intake.relay is None or intake.relay.id != row.source_relay_id:
                raise ValueError("collaboration runtime changed before dispatch")
            from coreman.core.db.models import ChatSession

            current = await session.get(
                ChatSession, (intake.bot.id, row.source_session_key), populate_existing=True
            )
            ttl = int(await ctx.settings_store.get("session_ttl_hours", default=72))
            if (
                row.status != "resuming"
                or current is None
                or current.relay_session_id != row.source_relay_session_id
                or current.backend != backend
                or current.last_active_at < datetime.now(UTC) - timedelta(hours=ttl)
            ):
                raise ValueError("collaboration source session changed or expired")
            current.last_active_at = datetime.now(UTC)
            current.last_speaker_user_id = intake.speaker.user_id
            return sessions.SessionInfo(row.source_relay_session_id, False, False)
        ttl = int(await ctx.settings_store.get("session_ttl_hours", default=72))
        return await sessions.get_or_create(
            session,
            bot_id=intake.bot.id,
            session_key=intake.session_key,
            backend=backend,
            ttl_hours=ttl,
            speaker_user_id=intake.speaker.user_id,
        )

    async def _system_prompt(
        self,
        ctx: TaskContext,
        intake: Intake,
        info: sessions.SessionInfo,
        backend: str,
        systems_prompt: str = "",
    ) -> str:
        """本轮的 system prompt。续跑时也使用当前身份、配置与系统授权。"""
        return build_system_prompt(
            segments=await load_segments(ctx.settings_store),
            backend=backend,
            verbosity_level=intake.bot.verbosity_level,
            bot_prompt=intake.bot.merged_system_prompt,
            speaker=intake.speaker,
            speaker_changed=info.speaker_changed,
            systems_prompt=systems_prompt,
        )

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """覆盖 `open_stream` 的关键字参数（流 id、投递模式、回复上下文）。默认不覆盖。"""
        if ctx.task.payload.get("collaboration_phase") == "helper":
            return {
                "reply_context": {**intake.inbound.reply_context, "_collaboration_helper": True}
            }
        return {}

    def _after_supervisor(self, pre: Prepared) -> None:
        """超时看护器建好之后的挂钩。默认什么都不做（两阶段超时照常从头计时）。"""

    def _queued_seconds(self, ctx: TaskContext) -> int | None:
        """排队超过 5 秒才提示——1 秒的兜底轮询本来就会带来零点几秒的延迟。"""
        claimed, run_after = ctx.task.claimed_at, ctx.task.run_after
        if claimed is None or run_after is None:
            return None
        waited = (claimed - run_after).total_seconds()
        return int(waited) if waited > self.QUEUED_NOTICE_SECONDS else None
