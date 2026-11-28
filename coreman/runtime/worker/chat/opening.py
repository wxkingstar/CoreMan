"""开流阶段：等被替代的任务退出，一次建好会话、提示词、env、请求体与 task_streams。"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import groups as api_groups
from coreman.core.api_tasks.service import SILENT_KEY
from coreman.core.assistants import platform_turns
from coreman.core.assistants.approvals import batch as assistant_writes
from coreman.core.assistants.kinds import BOT_HELP_KIND, COLD_START_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.auth.system_access import build_system_access, mcp_runtime_supported
from coreman.core.auth.token_issues import record_issues
from coreman.core.bus import tasks
from coreman.core.chat import alert_sources, chat_logs, sessions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.redaction import collect_secrets
from coreman.core.cloud_proxy import confirm as cloud_confirm
from coreman.core.db.models import (
    CHAT_LOG_RUNNING,
    BackgroundRun,
    Bot,
    RelayServer,
    RuntimeNode,
    Task,
    User,
)
from coreman.core.i18n.messages import msg
from coreman.core.knowledge import memory
from coreman.core.knowledge.installation import effective_env
from coreman.core.prompting import (
    Speaker,
    build_env,
    build_system_prompt,
    build_turn_context,
    env_keys_for_log,
    identity_tag,
    load_segments,
    sanitize_user_input,
)
from coreman.core.relay.client import ChatRequest
from coreman.core.relay.labels import relay_label
from coreman.core.relay.models import backend_of
from coreman.core.runtime_nodes import dev_preview
from coreman.core.systems_catalog import confirm as systems_confirm
from coreman.core.systems_catalog import policy as catalog_policy
from coreman.runtime.worker.background import TimeoutSupervisor
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Intake, Prepared
from coreman.runtime.worker.chat.records import log_entry
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import open_stream
from coreman.runtime.worker.stream_writer import StreamWriter


def session_viewer_url(ctx: TaskContext, relay: RelayServer, relay_session_id: uuid.UUID) -> str:
    """会话查看链接：运行时一律由节点提供，查看器走管理台对该节点的反向代理。"""
    return (
        f"{ctx.public_base_url}/api/admin/runtime-nodes/{relay.runtime_node_id}"
        f"/{relay.model_provider}/session/{relay_session_id}"
    )


async def session_link(
    session: AsyncSession,
    ctx: TaskContext,
    intake: Intake,
    relay: RelayServer,
    relay_session_id: uuid.UUID,
) -> str:
    """本轮回复里的会话查看链接。

    查看页按对话记录判断归属：开流时这一轮已经写进 chat_logs（见 `_open`），第一轮也不例外，
    链接不需要另带凭据。群聊沿用管理员查看，私聊（含单人群）归本人。飞书私聊只有本人能看：发言人没绑定
    员工身份时谁也打不开，就不给。个人助理的会话查看页对谁都不开，也不给。
    """
    url = session_viewer_url(ctx, relay, relay_session_id)
    if intake.bot.kind == "assistant":
        return ""
    if (
        intake.private
        and intake.bot.platform == "feishu"
        and (intake.speaker.user_id is None or relay.runtime_node_id is None)
    ):
        return ""
    return url


async def previous_speaker(session: AsyncSession, info: sessions.SessionInfo) -> Speaker | None:
    """同一会话里换了人时，上一轮的发言者：本轮块的换人提醒要点名前后两个人。"""
    if info.previous_speaker_user_id is None:
        return None
    user = await session.get(User, info.previous_speaker_user_id)
    login = user.login_name if user else None
    name = user.display_name if user else None
    return Speaker(
        "", info.previous_speaker_user_id, login, name or (None if login else "上一位发言者")
    )


LIVE_JOINED_NOTE = (
    "- 本轮写进了后台阶段里还在运行的同一个进程：业务系统令牌、凭据与环境沿用开工那一轮，"
    "本轮没有重新签发；后台任务的实际状态以会话里的任务通知为准。"
)


@dataclass(frozen=True)
class LiveMode:
    """这一轮的后台阶段参数：发给驱动的 owner 与到期时间；`run` 非空表示写进正在后台阶段的进程。"""

    owner: str
    deadline: int
    run: BackgroundRun | None = None
    secrets: frozenset[str] = frozenset()

    @property
    def joined(self) -> bool:
        return self.run is not None


SOLO_NOTE = (
    "- 这个群里此刻只有提问人自己（其余都是机器人），这一轮按私聊处理：本人飞书工具可用，"
    "照私聊的规则做事。"
)
MOBILE_NOTE = (
    "- 提问人这条消息是在手机上发的：结论放最前，段落短，表格不超过 3 列，长内容放进可折叠段落。"
)


class OpenStage(ChatStageBase):
    """开流：同会话串行、会话与提示词、task_streams 与超时看护。"""

    async def _wait_superseded(self, ctx: TaskContext, bot_id: uuid.UUID, session_key: str) -> bool:
        """等同会话里比本任务早的活动任务都退出，免得两轮同时往同一个 relay 会话里写。

        返回 False 表示等待期间本任务自己被取消（用户 stop / 又来了更新的消息）；等满
        `SUPERSEDE_WAIT_SECONDS` 仍未退出抛 `TimeoutError`，由调用方决定重排还是放弃。

        只等比自己早的任务，不只等这一次标记的：重排回来再认领时，那些任务早就带着取消标记、
        `supersede` 不会再返回它们，但串行不变量照样要守。比自己晚的任务会反过来替代本任务，
        这里靠 `cancel_event` 退出，两边不会互等。被替代任务所在的 worker 失联时，reaper
        约一分钟内收尸，等待上限留足了这段时间。
        """
        deadline = time.monotonic() + self.SUPERSEDE_WAIT_SECONDS
        delay = self.SUPERSEDE_POLL_SECONDS
        while True:
            async with ctx.session_factory() as session:
                active = await tasks.active_for_session(session, bot_id, session_key)
            older = [row.id for row in active if row.id < ctx.task.id]
            if not older:
                return True
            if ctx.cancel_event.is_set():
                return False
            if time.monotonic() >= deadline:
                ctx.log.warning("superseded_wait_timeout", victims=older)
                raise TimeoutError("session_busy")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ctx.cancel_event.wait(), delay)
            delay = min(delay * 2, self.SUPERSEDE_POLL_MAX_SECONDS)

    async def _read_group_cards(self, ctx: TaskContext, intake: Intake) -> None:
        """开轮之前把群聊记录里别的机器人卡片的正文读好：开轮时持有 bot 行锁，不能去飞书读。

        只是让群聊记录更完整，读不到或出了意外都不拦这一轮。
        """
        from coreman.core.chat import group_observe

        if intake.chat_type != "group" or not group_observe.observing(intake.bot):
            return
        try:
            async with ctx.session_factory() as session:
                lines = await group_observe.transcript(session, intake.bot, intake.chat_id)
            await group_observe.read_cards(lines, intake.bot, intake.chat_id, ctx.cipher)
        except Exception as exc:  # noqa: BLE001 见上
            ctx.log.warning("group_cards_read_failed", error_type=type(exc).__name__)

    async def _wait_transcript(self, ctx: TaskContext, intake: Intake) -> bool:
        """个人助理：上一轮被取消、它的转录还在从节点传回来的路上时，等它到了再开轮。

        不等的话，`transcripts.prepare` 看到交接没办完就换新会话，所有者每打断一次助理，当天
        的上下文就丢一次。每次查询各用一个短事务，等待期间不占连接、不持锁；最多等到
        `transcripts.UPLOAD_WAIT`，到点还没来就照旧换新会话。返回 False 表示等待期间本任务
        自己被取消。查询出了意外不拦这一轮：不等，按原来的办。
        """
        from coreman.core.assistants import transcripts

        delay = self.TRANSCRIPT_POLL_SECONDS
        waited = False
        while True:
            try:
                async with ctx.session_factory() as session:
                    until = await transcripts.awaiting_upload(
                        session,
                        bot_id=intake.bot.id,
                        session_key=intake.session_key,
                        task_id=ctx.task.id,
                    )
            except Exception as exc:  # noqa: BLE001 等不等只影响续不续得上，不该让这一轮开不了
                ctx.log.warning("assistant_transcript_wait_failed", error_type=type(exc).__name__)
                return True
            if until is None:
                if waited:
                    ctx.log.info("assistant_transcript_wait_ended")
                return True
            if ctx.cancel_event.is_set():
                return False
            waited = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ctx.cancel_event.wait(), delay)
            delay = min(delay * 2, self.TRANSCRIPT_POLL_MAX_SECONDS)

    async def _open(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        intake: Intake,
        relay: RelayServer,
        started: float,
        *,
        reuse: Prepared | None = None,
        allow_join: bool = True,
    ) -> Prepared:
        """会话、提示词、env、请求体与 task_streams 一次建好。

        `reuse` 是重发：要求接进后台阶段的一轮被驱动拒收（会话刚好结束），按普通一轮重建请求
        （照常签发凭据），沿用已经开好的流与对话记录。`allow_join=False` 时不再尝试接进去。
        """
        # 换机事务持有 bot 行锁；等待提交后从当前实例构造请求，避免派发到旧实例。
        bot = await session.scalar(select(Bot).where(Bot.id == intake.bot.id).with_for_update())
        current_relay = (
            await session.get(RelayServer, bot.relay_server_id)
            if bot and bot.relay_server_id
            else None
        )
        if bot is None or not bot.enabled or current_relay is None or not current_relay.is_active:
            raise ValueError("bot_or_relay_changed_before_dispatch")
        # FOR KEY SHARE 持有到开流提交：收尸（FOR UPDATE SKIP LOCKED）会跳过这个任务，不会在检查
        # 之后、写进行中记录之前插进来结掉它（否则收尸补一行、这里再写一行）；心跳只改非键列，
        # 不受影响。收尸先拿到锁的，这里等它提交后读到的就是终态。
        current_task = await session.get(
            Task,
            ctx.task.id,
            with_for_update={"read": True, "key_share": True},
            populate_existing=True,
        )
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
        if ctx.task.payload.get("human_collaboration_id"):
            from coreman.core.db.models import HumanCollaboration

            asked = await session.get(
                HumanCollaboration,
                uuid.UUID(ctx.task.payload["human_collaboration_id"]),
                populate_existing=True,
            )
            if asked is None or asked.status != "resuming" or asked.resume_task_id != ctx.task.id:
                raise ValueError("collaboration cancelled before dispatch")
        relay = current_relay
        intake = replace(intake, bot=bot, relay=relay)
        if intake.speaker.known:
            # A service user has no platform identity to look up, so an alert turn (and a
            # business system's group delivery) rechecks the user itself.
            fresh_speaker = (
                await alert_sources.refresh_speaker(session, intake.speaker)
                if ctx.task.payload.get("origin") == alert_sources.ORIGIN
                or api_groups.is_group_payload(ctx.task.payload)
                else await resolve_speaker(
                    session, platform=bot.platform, platform_user_id=intake.speaker.platform_user_id
                )
            )
            if fresh_speaker.user_id != intake.speaker.user_id:
                fresh_speaker = Speaker(intake.speaker.platform_user_id, None, None, None)
            intake = replace(intake, speaker=fresh_speaker)
        writer = reuse.writer if reuse is not None else StreamWriter(ctx)
        queued = self._queued_seconds(ctx) if reuse is None else None
        if queued is not None:
            writer.set_thinking_line(msg("queued_notice", ctx.locale, seconds=queued))
        backend = backend_of(bot.model, relay.model_provider)
        info = await self._session_info(session, ctx, intake, backend)
        transcript = None
        if is_assistant(bot):
            from coreman.core.assistants import transcripts
            from coreman.runtime.worker.chat.assistant_runtime import AssistantDispatchError

            if relay.runtime_node_id is None:
                raise ValueError("bot_or_relay_changed_before_dispatch")
            # 个人助理的一轮在节点的沙箱里跑：定下续不续上一轮的转录（续不上就换新会话）。
            # 之后用到会话的地方都用 prepare 返回的这一份；出意外就不派。
            try:
                info, transcript = await transcripts.prepare(
                    session,
                    bot=bot,
                    node_id=relay.runtime_node_id,
                    info=info,
                    session_key=intake.session_key,
                    backend=backend,
                    speaker_user_id=intake.speaker.user_id,
                )
            except Exception as exc:
                ctx.log.warning(
                    "assistant_transcript_prepare_failed", error_type=type(exc).__name__
                )
                raise AssistantDispatchError from None
        live = await self._live_mode(
            session,
            ctx,
            intake,
            relay,
            backend,
            info,
            sandbox=transcript is not None,
            allow_join=allow_join,
        )
        joined = live is not None and live.joined
        live_origin = ""
        if joined:
            assert live is not None and live.run is not None
            from coreman.core.chat import background_runs
            from coreman.runtime.worker.chat.finalize import _question

            live_origin = _question(await background_runs.origin_text(session, live.run))
            current_task.payload = {**current_task.payload, "live_join": live.run.id}
            ctx.task.payload = {**ctx.task.payload, "live_join": live.run.id}
        # An alert turn is started by external text with nobody watching, so read-only is enforced
        # by withholding the means to write: bot and skill env, the cloud proxy, the catalog MCP.
        alert_turn = ctx.task.payload.get("origin") == alert_sources.ORIGIN
        # 个人助理里平台触发的一轮（含带入站事件的审批结果回合）无人在场，输入可能来自别人发给
        # 所有者的消息：业务系统令牌、业务系统 MCP、操作目录与云签名代理照常以所有者身份挂上，
        # 但不能当场写——经云签名代理与目录代理的写由服务端暂存并提交所有者审批（不能提审批的
        # 轮次拒绝），持令牌直连的系统（含 MCP）只靠本轮块的只读说明；所有者的个人凭证不挂。
        # 冷启动归纳口吻的那一轮用不到这些，一样都不挂。机器人自己配的静态环境变量不受影响。
        unattended = platform_turns.unattended(ctx.task)
        cold_start = ctx.task.kind == COLD_START_KIND
        # 没人点名的轮次（旁听插话、调度）由没人确认过的群消息触发，却会以消息作者的身份拿到
        # 令牌：令牌会进运行环境的系统（env、mcp）这类轮次不签发，平台代理的系统照常（写由服务端
        # 把住）。员工开了「没人点名时也下发」的照旧。告警轮次不在其内：它以管理员配置的服务用户
        # 身份运行，又不挂操作目录，停掉令牌就完全查不了业务系统。
        origin = ctx.task.payload.get("origin")
        unprompted = origin in ("observe", "dispatch") and not bot.unprompted_system_tokens
        catalog_mount = (
            None
            if alert_turn or cold_start or joined
            else await catalog_policy.mount_for(
                session,
                relay=relay,
                model=bot.model,
                task_id=ctx.task.id,
                public_base_url=ctx.public_base_url,
                ttl_seconds=bot.sse_timeout_seconds,
            )
        )
        mcp_ok = await mcp_runtime_supported(session, relay=relay, model=bot.model)
        access = await build_system_access(
            session,
            ctx.cipher,
            bot=bot,
            speaker=intake.speaker,
            issuer=str(await ctx.settings_store.get("jwt_issuer", default="coreman")),
            external_key=ctx.external_jwt_key,
            providers=ctx.business_token_providers,
            catalog=catalog_mount,
            mcp_supported=mcp_ok,
            unattended=unattended,
            # 写进后台阶段的进程：环境沿用开工那一轮，本轮签发了也用不上，还会留下误导的签发记录。
            withhold=cold_start or joined,
            unprompted=unprompted,
            # 调度员只安排不干活，本来就不用这些系统：不必告诉它「本轮不可用」。
            unprompted_note=origin != "dispatch",
        )
        # 和本轮对话记录同一事务提交：业务系统日志里的 token_id 按它查回这一轮。
        await record_issues(
            session,
            access.issued,
            purpose="chat",
            subject=access.subject or "",
            task_id=ctx.task.id,
            bot_id=bot.id,
            user_id=intake.speaker.user_id,
        )
        env = build_env(
            bot_key=bot.bot_key,
            platform=bot.platform,
            chat_id=intake.chat_id,
            # 单人群按私聊：运行节点只在 single / cron 时挂本人飞书工具（驱动的兜底检查）。
            chat_type="single" if intake.solo else intake.chat_type,
            platform_user_id=intake.speaker.platform_user_id,
            session_id=str(info.relay_session_id),
            speaker=intake.speaker,
            bot_env={} if alert_turn else await effective_env(session, ctx.cipher, bot),
        )
        if ctx.task.payload.get("origin") != "dispatch":
            # 调度员只安排不干活：调度回合不挂业务系统令牌。
            env.update(access.env)
        # 各处把会变的本轮状态写进 ctx.turn_notes，组成本轮块；重新开流时从头攒。
        ctx.turn_notes = [LIVE_JOINED_NOTE if joined else access.note]
        if alert_turn:
            ctx.turn_notes.append(alert_sources.ALERT_NOTE)
        if intake.solo:
            ctx.turn_notes.append(SOLO_NOTE)
        from coreman.core.chat import group_observe

        if intake.chat_type == "group" and group_observe.observing(bot):
            # 开了旁听的机器人看得到整段群聊：@ 它的回合和主动插话的回合都带上最近的记录。
            # 本轮这条消息已在正文里，记录里去掉，免得模型分不清哪条是本轮要处理的。
            interject = ctx.task.payload.get("origin") == "observe"
            # 调度回合和插话一样，记录之后那条消息就是要安排的事。
            # 插话和调度回合处理的是一批消息：把这一批（判断时的起点之后）标成【新】。
            since = ctx.task.payload.get("observe_since")
            lines = [
                line
                for line in await group_observe.transcript(
                    session,
                    bot,
                    intake.chat_id,
                    new_since=datetime.fromisoformat(since) if since else None,
                )
                if line.event_id is None or line.event_id != ctx.task.inbound_event_id
            ]
            # 别的机器人发的卡片事件里没有正文：用开轮前读好的（这里持有 bot 行锁，不发请求）。
            lines = group_observe.known_cards(lines, bot, intake.chat_id)
            ctx.turn_notes.append(group_observe.transcript_note(lines, interject=interject))
            if interject:
                ctx.turn_notes.append(group_observe.INTERJECT_NOTE)
            elif ctx.task.payload.get("origin") == "dispatch":
                # 这一批消息提到的任务编号：从本轮这条、再从新到旧看这一批。
                texts = [intake.text, *(line.text for line in reversed(lines) if line.new)]
                ctx.turn_notes.append(await self._dispatch_note(session, ctx, intake, texts))
        from coreman.runtime.worker.chat import work

        if work.code_of(intake.session_key):
            # 调度员派的任务：每轮都带上任务和回复规则，再记一笔「开始处理」。
            await work.drive_follow_up(session, ctx, intake)
            work_note = await work.note(session, ctx, intake)
            if work_note:
                ctx.turn_notes.append(work_note)
                await work.mark_started(session, ctx, intake)
        inbound_context = intake.inbound.reply_context or {}
        if (
            intake.bot.platform == "feishu"
            and intake.chat_type == "single"
            and inbound_context.get("client") == "mobile"
        ):
            # 私聊里提问人在手机上：本轮提示，不进 system prompt（设备每轮都可能变）。
            ctx.turn_notes.append(MOBILE_NOTE)
        if ctx.task.payload.get("stopped_previous"):
            from coreman.core.prompting.defaults import STOPPED_PREVIOUS_NOTE

            ctx.turn_notes.append(STOPPED_PREVIOUS_NOTE)
        written = ctx.task.payload.get(systems_confirm.RESULT_KEY)
        if isinstance(written, str):
            # 确认卡点完后的续接：平台按确认内容执行的结果。
            ctx.turn_notes.append("- 业务系统写操作：" + written)
        cloud_written = ctx.task.payload.get(cloud_confirm.RESULT_KEY)
        if isinstance(cloud_written, str):
            # 个人助理的云服务写操作确认卡点完后的续接：平台按暂存内容执行的结果。
            ctx.turn_notes.append("- 云服务写操作：" + cloud_written)
        feishu_written = ctx.task.payload.get(assistant_writes.RESULT_KEY)
        if isinstance(feishu_written, str):
            # 个人助理的飞书写操作确认卡点完后的续接：平台按暂存内容执行的各项结果。
            ctx.turn_notes.append("- 飞书写操作：" + feishu_written)
        if ctx.task.payload.get("api_task_id"):
            from coreman.runtime.worker.chat import api

            api_note = await api.note(session, ctx)
            if api_note:
                ctx.turn_notes.append(api_note)
        if is_assistant(bot):
            # 个人知识库：会话首轮给置顶页与索引，之后只在置顶页有更新时补一条说明。
            from coreman.runtime.worker.chat import assistant_notes

            knowledge = await assistant_notes.turn_note(session, ctx, intake, info)
            if knowledge:
                ctx.turn_notes.append(knowledge)
            from coreman.core.assistants import digest

            if ctx.task.kind == "chat" and await digest.owner_private_chat(
                session,
                bot,
                user_id=intake.speaker.user_id,
                chat_type=intake.chat_type,
                chat_id=intake.chat_id,
            ):
                # 所有者在私聊里发来「现在处理 F012」（摘要上的按钮）：附上这条待跟进。
                # 别人或群里发的同一句话什么都不附：不泄露内容，也不冒充所有者在场。
                handle = await digest.now_note(session, bot.id, intake.text)
                if handle:
                    ctx.turn_notes.append(handle)
                # 所有者发来「开通 G003」（看板上的按钮）：附上这个缺口与怎么开。
                from coreman.core.assistants import gaps

                opening_gap = await gaps.open_note(session, bot.id, intake.text)
                if opening_gap:
                    ctx.turn_notes.append(opening_gap)
        resume_fallback = ""
        if info.inherited_from is not None:
            from coreman.core.chat.session_carry import carried_context

            carried = await carried_context(
                session,
                bot_id=bot.id,
                session_key=intake.session_key,
                relay_session_id=info.inherited_from,
            )
            if carried and info.inherited_from != info.relay_session_id:
                ctx.turn_notes.append(carried)
            elif carried:
                # 会话文件已带到新运行时，原生续聊；驱动续不上时才把它放进本轮块。
                resume_fallback = carried
        fork_session_id = ""
        if info.forked_from is not None:
            from coreman.core.chat import topics
            from coreman.core.chat.session_carry import carried_context

            forked = await carried_context(
                session,
                bot_id=bot.id,
                session_key=topics.chat_of(intake.session_key),
                relay_session_id=info.forked_from,
                header=topics.FORK_HEADER,
            )
            if await topics.fork_supported(session, relay.runtime_node_id, backend):
                # 运行时原生分叉；群主会话在这台节点上找不到时，驱动用这段开新会话。
                fork_session_id = str(info.forked_from)
                resume_fallback = forked or resume_fallback
            elif forked:
                ctx.turn_notes.append(forked)
        # 各处在传入文本后面追加自己的规则：从空串起步，攒出的就是本轮附加能力，
        # 交给 build_system_prompt 放在结尾重申之前。
        from coreman.runtime.worker.chat import wecom_personal

        extra = ""
        personal_secrets: frozenset[str] = frozenset()
        # 个人助理调度来的一轮：所有者不在场，凡是要他当场点卡片、或以他的飞书 / 企微身份行事的
        # 能力都不挂（个人工具、本人定时任务、群文档、向员工或同事求助、凭证索取）；只注入他在
        # 这位员工名下已保存的凭证，并在本轮块里写明这一轮怎么作答。
        bot_help = ctx.task.kind == BOT_HELP_KIND
        api_silent = bool(ctx.task.payload.get(SILENT_KEY))
        if bot_help:
            from coreman.core.assistants import dispatch_prompt
            from coreman.runtime.worker.chat import credentials

            ctx.turn_notes.append(
                dispatch_prompt.target_note(allow_write=ctx.task.payload.get("allow_write") is True)
            )
            extra, env, personal_secrets = await credentials.stored_only(
                session, ctx, intake, extra, env
            )
        elif api_silent:
            # 静默执行的业务系统任务：没有人能当场点卡、填表或接同事的话。和调度来的一轮一样，
            # 只注入员工已保存的凭证，求助同事、个人工具、定时任务、群文档、任务平台、凭证索取
            # 这些要找人或往飞书发东西的能力都不挂。
            from coreman.runtime.worker.chat import credentials

            extra, env, personal_secrets = await credentials.stored_only(
                session, ctx, intake, extra, env
            )
        elif (
            not joined
            and ctx.task.payload.get("origin")
            not in (
                "observe",
                "dispatch",
                alert_sources.ORIGIN,
            )
            # 群承接模式以服务账号身份跑：和告警回合一样，不挂以发言者本人为前提的能力，
            # 也不向服务账号索要个人凭证。
            and not api_groups.is_group_payload(ctx.task.payload)
        ):
            # 主动插话只答不做：求助同事、个人工具、定时任务、索要凭证这些有后果的能力都不挂，
            # 真要做就由它认领、等对方 @ 它，走正常回合。
            from coreman.runtime.worker.chat.collaboration import configure

            extra, env = await configure(session, ctx, intake, info, extra, env)
            from coreman.runtime.worker.chat.personal import configure as configure_personal

            extra, env = await configure_personal(
                session, ctx, intake, info.relay_session_id, extra, env
            )
            extra, env = await wecom_personal.configure(
                session, ctx, intake, info.relay_session_id, extra, env
            )
            from coreman.runtime.worker.chat import schedules

            extra, env = await schedules.configure(
                session, ctx, intake, info.relay_session_id, extra, env
            )
            from coreman.runtime.worker.chat import feishu_docs

            extra, env = await feishu_docs.configure(
                session, ctx, intake, info.relay_session_id, extra, env
            )
            from coreman.runtime.worker.chat import work_tools

            if work_tools.mount(
                env,
                cipher=ctx.cipher,
                bot=bot,
                task_id=ctx.task.id,
                actor_id=intake.speaker.user_id,
                kind="chat",
                public_base_url=ctx.public_base_url,
                mcp_supported=mcp_ok and not unattended,
            ):
                from coreman.core.work_tasks import agent_tools
                from coreman.core.work_tasks import tools as work_task_tools

                extra += work_task_tools.POLICY
                if intake.session_key.startswith("work:"):
                    extra += agent_tools.TASK_POLICY
            from coreman.runtime.worker.chat import credentials

            if unattended:
                extra, env = credentials.withhold(ctx, extra, env)
            else:
                extra, env, personal_secrets = await credentials.configure(
                    session, ctx, intake, extra, env, relay_session_id=info.relay_session_id
                )
        from coreman.runtime.worker.chat import cloud_proxy

        env = await cloud_proxy.configure(
            session,
            ctx,
            bot=bot,
            # 个人助理调度来的一轮不挂云签名代理：它的写操作要所有者另行审批，结果回不到助理。
            # 助理无人在场的一轮挂上，写由代理按任务类型拒绝；冷启动那一轮不挂。
            # 静默执行的业务系统任务也不挂：云产品写操作要在私聊里点确认卡。
            user_id=None
            if alert_turn or cold_start or bot_help or joined or api_silent
            else intake.speaker.user_id,
            origin_kind="chat",
            env=env,
            session_key=intake.session_key,
            relay_session_id=info.relay_session_id,
            event_id=intake.inbound.id,
        )
        if is_assistant(bot):
            # 个人助理：本轮挂了哪些业务系统（读写级别）与云产品写进本轮块，已可用的缺口记为解决。
            # 告警与冷启动不是替所有者办事的一轮，清单不列、也不比对缺口。级别按这一轮实际下发的
            # 说（没人点名、身份不可用、签发失败的标成没下发）；调度回合什么都不挂进运行环境。
            from coreman.core.assistants import capabilities

            ctx.turn_notes.append(
                await capabilities.apply(
                    session,
                    bot.id,
                    mode="withheld"
                    if alert_turn or cold_start
                    else "unattended"
                    if unattended
                    else "attended",
                    mcp_supported=mcp_ok,
                    mounted=catalog_mount is not None,
                    cloud_attached=ctx.cloud_proxy_ready
                    and intake.speaker.user_id is not None
                    and not (alert_turn or cold_start or bot_help),
                    deployment=ctx,
                    delivered=frozenset() if origin == "dispatch" else access.delivered,
                )
            )
        from coreman.core.prompting.rich_cards import with_reply_files, with_reply_images
        from coreman.runtime.worker.chat.reply_files import ReplyFiles

        # 交给平台解析的回答（helper、个人助理调度来的一轮）不会发到聊天里，图片说明用不上。
        # 静默执行的业务系统任务只回文字给业务系统，图片和附件发不到任何人。
        if not self._structured_reply(ctx) and not api_silent:
            extra = with_reply_images(extra, intake.bot.platform, backend)
            if ReplyFiles.applies(intake.bot) and origin not in ("observe", "dispatch"):
                extra = with_reply_files(extra)
        tag = identity_tag(ctx.cipher, info.relay_session_id)
        system_prompt = await self._system_prompt(
            ctx,
            intake,
            backend,
            tag,
            access.prompt,
            extra,
            memory_dir=await memory.prompt_dir(session, relay, backend, bot.working_dir),
            dev_preview=bot.dev_mode and await dev_preview.supported(session, relay, backend),
            background_tasks=await self._background_prompt(session, intake, relay, backend),
        )
        turn_context = build_turn_context(
            tag=tag,
            speaker=intake.speaker,
            previous=await previous_speaker(session, info),
            notes=ctx.turn_notes,
        )
        # 个人助理调度来的一轮：记在这位员工名下，但只有所有者本人看得到内容。
        # 单人群按私聊：对话记录和私聊一样只有本人能看。
        ctx.private_turn = (
            wecom_personal.mounted(env) or ctx.task.kind == BOT_HELP_KIND or intake.solo
        )
        # env 到这里才齐（业务系统 + 协作 + 两套个人工具 + 本人定时任务 + 个人凭证），
        # 出站闸门必须按最终的这一份建；个人凭证按值强制脱敏，不看键名。
        # 写进后台阶段的进程时，进程里还是开工那一轮的凭据：出站按那一份一起过滤。
        ctx.secrets = (
            collect_secrets(env) | personal_secrets | (live.secrets if live else frozenset())
        )
        session_url = await session_link(session, ctx, intake, relay, info.relay_session_id)
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
            env_vars=env,
            turn_context=turn_context,
            worktree=bot.dev_mode,
            resume_fallback=resume_fallback,
            fork_session_id=fork_session_id,
            sandbox=transcript is not None,
            transcript=transcript.body() if transcript is not None else None,
            live_owner=live.owner if live else "",
            live_deadline=live.deadline if live else None,
            live_join=joined,
        )
        if reuse is not None:
            # 正文沿用已经组装好的那一份（含下载好的媒体）。
            request = replace(request, user_content=reuse.request.user_content)
            return replace(reuse, intake=intake, relay=relay, info=info, request=request)
        reply_context = dict(intake.inbound.reply_context or {})
        if bot.platform == "feishu":
            # 飞书回复卡片里的回复按钮只有这一轮的提问人能点：网关编译终稿时从这里取。
            reply_context["requester_user_id"] = intake.speaker.platform_user_id
            if code := work.code_of(intake.session_key):
                # 回复存档时记下任务编号：有人回复这条，就续进这个任务的会话。
                reply_context["_work_code"] = code
        stream_kwargs: dict[str, Any] = {
            "reply_context": reply_context,
            "session_url": session_url,
            "platform": bot.platform,
        }
        stream_kwargs.update(self._stream_kwargs(ctx, intake))
        await open_stream(session, ctx, **stream_kwargs)
        # 这一轮的记录与会话、流同一事务提交：链接一出现在聊天里，查看页就能按它判断归属。
        # 放在开流之后：记录要带上流 id。
        await chat_logs.open_turn(
            session,
            log_entry(ctx, intake, status=CHAT_LOG_RUNNING, relay_session_id=info.relay_session_id),
        )
        if (
            bot.platform == "feishu"
            and ctx.task.inbound_event_id is not None
            and ctx.task.inbound_event_id == intake.inbound.id
        ):
            from coreman.core.chat.card_replies import enqueue_used

            # 点回复按钮带来的这一轮过了入站的各道关、真正开始了，才把那一行按钮置灰；
            # 被拒的点击（停用、白名单、命令）不改卡片。
            await enqueue_used(session, bot.id, intake.inbound)
        if (
            info.is_new
            and bot.platform != "feishu"
            and ctx.task.payload.get("collaboration_phase") != "helper"
        ):
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
        pre = Prepared(
            intake,
            relay,
            info,
            request,
            writer,
            session_url,
            started,
            supervisor,
            relay_label=relay_label(
                relay,
                await session.get(RuntimeNode, relay.runtime_node_id)
                if relay.runtime_node_id
                else None,
            ),
            live_origin=live_origin,
        )
        self._after_supervisor(pre)
        if ctx.task.payload.get("work_silent") or ctx.task.payload.get(SILENT_KEY):
            # 子任务、静默执行的业务系统任务这样不出声的一轮：进度与超时通知也不推。
            pre.supervisor.silent = True
        return pre

    def _joinable(self, ctx: TaskContext, intake: Intake) -> bool:
        """这一轮能不能进（或写进）后台阶段：飞书上普通员工、本人当面发起、续同一会话的轮次。

        主动插话、调度、告警、协作、API、个人助理这些回合要么没人可通知，要么回复交给平台解析，
        按原来的一轮一进程跑；会话正在后台阶段时它们排队等（见 `_hold_for_background`）。
        """
        from coreman.core.chat import background_runs

        payload = ctx.task.payload
        return not (
            ctx.task.kind not in background_runs.OWNER_KINDS
            or intake.bot.platform != "feishu"
            or is_assistant(intake.bot)
            or not intake.speaker.platform_user_id
            or self._structured_reply(ctx)
            or payload.get("origin") in ("observe", "dispatch", alert_sources.ORIGIN)
            or payload.get("collaboration_id")
            or payload.get("human_collaboration_id")
            or payload.get("api_task_id")
        )

    async def _live_mode(
        self,
        session: AsyncSession,
        ctx: TaskContext,
        intake: Intake,
        relay: RelayServer,
        backend: str,
        info: sessions.SessionInfo,
        *,
        sandbox: bool,
        allow_join: bool = True,
    ) -> LiveMode | None:
        """这一轮的后台阶段参数：写进正在后台阶段的进程，或者带上 owner 与到期时间开新进程。

        会话正在后台阶段、本人能接时一律写进去（不看节点此刻的能力位，进程已经在那里）；
        `allow_join=False`（驱动刚说进程不在了）时按新进程开。
        """
        from coreman.core.chat import background_runs

        if sandbox or not self._joinable(ctx, intake):
            return None
        run = await background_runs.running_for(session, intake.bot.id, intake.session_key)
        if (
            allow_join
            and run is not None
            and run.close_requested_at is None
            and run.owner_platform_user_id == intake.speaker.platform_user_id
            and run.relay_session_id == info.relay_session_id
        ):
            return LiveMode(
                intake.speaker.platform_user_id,
                int(run.deadline.timestamp()),
                run,
                background_runs.open_secrets(ctx.cipher, run),
            )
        if not await background_runs.supported(session, relay, backend):
            return None
        # 主回合加后台阶段一共不超过员工的任务超时：从这一轮开始算。
        return LiveMode(
            intake.speaker.platform_user_id,
            int(time.time()) + int(intake.bot.sse_timeout_seconds),
        )

    async def _background_prompt(
        self, session: AsyncSession, intake: Intake, relay: RelayServer, backend: str
    ) -> bool:
        """系统提示要不要带后台任务的两条规则：只看员工与节点（不看这一轮的来源），
        同一会话里各轮的稳定段保持一致，缓存不失效。"""
        from coreman.core.chat import background_runs

        return (
            intake.bot.platform == "feishu"
            and not is_assistant(intake.bot)
            and await background_runs.supported(session, relay, backend)
        )

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
            # _open holds the bot lock and has refreshed intake.relay. An intervening runtime
            # switch is fine while the session key still maps to the source session: the switch
            # keeps a mapping only when that session's files moved to the new runtime.
            if intake.relay is None:
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
        info = await sessions.get_or_create(
            session,
            bot_id=intake.bot.id,
            session_key=intake.session_key,
            backend=backend,
            ttl_hours=ttl,
            speaker_user_id=intake.speaker.user_id,
        )
        if self.DAILY_SESSIONS and is_assistant(intake.bot):
            from coreman.runtime.worker.chat import assistant_day

            info = await assistant_day.roll(
                session,
                intake.bot,
                info,
                session_key=intake.session_key,
                chat_id=intake.chat_id,
                backend=backend,
                speaker_user_id=intake.speaker.user_id,
            )
        return info

    async def _system_prompt(
        self,
        ctx: TaskContext,
        intake: Intake,
        backend: str,
        tag: str,
        systems_prompt: str = "",
        extra: str = "",
        memory_dir: str = "",
        dev_preview: bool = False,
        background_tasks: bool = False,
    ) -> str:
        """本轮的 system prompt（稳定段）。续跑时也使用当前配置与系统授权；身份在本轮块里。"""
        from coreman.core.prompting.rich_cards import rich_cards_prompt

        return build_system_prompt(
            segments=await load_segments(ctx.settings_store),
            backend=backend,
            verbosity_level=intake.bot.verbosity_level,
            bot_prompt=intake.bot.merged_system_prompt,
            tag=tag,
            systems_prompt=systems_prompt,
            extra=extra,
            output_format=rich_cards_prompt(intake.bot.platform, intake.bot.rich_cards),
            structured_reply=self._structured_reply(ctx),
            dev_mode=intake.bot.dev_mode,
            memory_dir=memory_dir,
            dev_preview=dev_preview,
            background_tasks=background_tasks,
        )

    async def _dispatch_note(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, texts: list[str]
    ) -> str:
        """调度回合的本轮块：群里能接任务的员工、这个群最近的任务和 `texts` 里提到的任务；
        员工名录与任务编号也存进上下文，收尾校验决定时用同一份。"""
        from coreman.core.chat import dispatch

        staff, unverified = await dispatch.employees(
            session, intake.bot, intake.chat_id, intake.speaker.user_id, ctx.cipher
        )
        recent = await dispatch.recent_tasks(session, intake.chat_id)
        referenced, hidden = await dispatch.referenced_tasks(
            session,
            dispatcher=intake.bot,
            chat_id=intake.chat_id,
            speaker_id=intake.speaker.user_id,
            texts=texts,
            exclude={t.code for t in recent},
        )
        ids = {t.assignee_bot_id for t in [*recent, *referenced] if t.assignee_bot_id}
        names = {b.id: b.name for b in await session.scalars(select(Bot).where(Bot.id.in_(ids)))}
        ctx.dispatch_staff = staff
        ctx.dispatch_codes = {t.code for t in recent}
        ctx.dispatch_referenced = {t.code for t in referenced}
        return dispatch.note(
            staff,
            unverified,
            recent,
            names,
            solo=bool(ctx.task.payload.get("solo")),
            mentioned=bool(ctx.task.payload.get("mentioned")),
            awaited=[str(code) for code in ctx.task.payload.get("awaited") or []],
            referenced=referenced,
            hidden=hidden,
            me=intake.bot.id,
            chat_id=intake.chat_id,
        )

    def _structured_reply(self, ctx: TaskContext) -> bool:
        """这一轮的回答是不是交给平台按固定格式解析、不给人看。默认只有协作的 helper 阶段。"""
        return ctx.task.payload.get("collaboration_phase") == "helper" or bool(
            ctx.task.payload.get("work_silent")
        )

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """覆盖 `open_stream` 的关键字参数（流 id、投递模式、回复上下文）。默认不覆盖。"""
        if ctx.task.payload.get("collaboration_phase") == "helper":
            return {
                "reply_context": {**intake.inbound.reply_context, "_collaboration_helper": True}
            }
        if ctx.task.payload.get("work_silent") or ctx.task.payload.get(SILENT_KEY):
            # 子任务：结果由平台交回上级任务；静默执行的业务系统任务：结果只经查询接口返回。
            # 这条流都不发到飞书。
            return {"reply_context": {**intake.inbound.reply_context, "_silent": True}}
        if ctx.task.payload.get("origin") == "observe":
            # 插话回合不推流式卡片：网关等终稿出来再发，终稿是「不说话」就什么也不发。
            return {"reply_context": {**intake.inbound.reply_context, "_observe": True}}
        if ctx.task.payload.get("origin") == "dispatch":
            # 调度回合同样只发终稿（安排的那一行）。开启时一开始就在原消息上打「处理中」表情，
            # 让发消息的人马上知道有人接了，终稿发出或决定不说话时撤掉；试运行群里不留痕迹。
            live = intake.bot.group_observe == "on"
            return {
                "reply_context": {
                    **intake.inbound.reply_context,
                    "_observe": True,
                    **({"_dispatch": True} if live else {}),
                }
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
