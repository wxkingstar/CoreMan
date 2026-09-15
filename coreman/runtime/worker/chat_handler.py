"""chat 任务处理器（spec §8.2 流水线、§8.3 请求构造、§8.4 事件与分类）。

流水线顺序即优先级：机器人停用 → 公告 → 白名单 → 内置命令 → 待答文本答案 / 取消 →
relay 不可用 → 同会话串行 → 会话/提示词/env → 开流 → 组装内容（下载媒体）→ 消费 SSE →
分类收尾。每一步要么放行到下一步，要么就地给用户一个回复并把任务结成终态，绝不留下
「流开着没人收尾」的状态。

组装内容排在开流之后：下载几十兆的附件要十几秒，「正在下载…」得有个思考区写进去，
用户才知道不是卡死了；媒体拿不到就在这里把这一轮结掉，不拿半条消息去问模型。

relay 检查排在最后：公告、拒绝、命令这几条路都不碰 relay，机器人没绑可用实例时它们照样
该答什么答什么，不该被一句「relay 未配置」顶掉。

进程内只有两条协程：主流程与 10 秒一次的心跳。取消（用户 stop / 被新消息替代 / 超时）
统一走 `ctx.cancel_event`，消费循环每一轮开头都看一眼；断开 relay 连接就等于让 relay
杀掉那一轮 CLI，所以取消时是 `aclose()` 生成器而不是干等它跑完。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import build_system_access
from coreman.core.bots.permissions import can_switch_relay
from coreman.core.chat import interactions, sessions
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.chat.commands import classify_command, is_cancel_word
from coreman.core.chat.content import BuiltContent, ContentBuilder
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.rate_limit import (
    is_rate_limited,
    pick_idle_server,
    quota_table,
    quota_warning,
)
from coreman.core.chat.session_switch import (
    PENDING_SECONDS,
    format_list,
    is_sessions_command,
    parse_choice,
    recent_sessions,
)
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    BotMember,
    InboundEvent,
    RelayServer,
    User,
)
from coreman.core.i18n.messages import msg
from coreman.core.knowledge.installation import effective_env
from coreman.core.prompting import (
    Speaker,
    build_env,
    build_system_prompt,
    env_keys_for_log,
    load_segments,
    sanitize_parts,
    sanitize_user_input,
)
from coreman.core.relay.client import ChatRequest
from coreman.core.relay.models import backend_of
from coreman.core.relay.sse import (
    AskUserQuestionEvent,
    FinishEvent,
    RelayErrorEvent,
    SseEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
    UsageEvent,
)
from coreman.core.timeutils import parse_ts
from coreman.core.wecom.cards import (
    choice_card,
    make_choice_prefix,
    make_ratelimit_prefix,
    question_brief,
    question_task_id,
    switch_offer_card,
)
from coreman.core.wecom.media import MediaFetcher
from coreman.runtime.bus import outbox, streams, tasks
from coreman.runtime.worker.background import DEFAULT_TIMING, TimeoutSupervisor, Timing
from coreman.runtime.worker.choice_flow import (
    FREE_TEXT_WAIT_SECONDS,
    advance_choice,
    record_answer,
)
from coreman.runtime.worker.commands import do_reset, do_stop
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound, open_stream, reply_once
from coreman.runtime.worker.stream_writer import StreamWriter


@dataclass(frozen=True)
class Intake:
    """入站解析的结果：够判断要不要继续，也够拼一条 chat_logs。

    `relay` 可空：机器人没绑 relay 也要留下一条 error 审计，这一步之后的流程才保证非空
    （见 `Prepared.relay`）。
    """

    bot: Bot
    relay: RelayServer | None
    inbound: InboundEvent
    speaker: Speaker
    chat_id: str
    chat_type: str
    session_key: str
    text: str
    message_type: str

    @property
    def relay_id(self) -> uuid.UUID | None:
        return self.relay.id if self.relay is not None else None


@dataclass(frozen=True)
class Prepared:
    """开流之后的全部上下文；`_converse` / `_finalize` 只读它。"""

    intake: Intake
    relay: RelayServer
    info: sessions.SessionInfo
    request: ChatRequest
    writer: StreamWriter
    session_url: str
    started_clock: float
    supervisor: TimeoutSupervisor
    # 开流之后才组装（下载要写提示行），所以这里可空：`_prepare` 交出去之前一定已填上。
    content: BuiltContent | None = None


@dataclass
class Outcome:
    """一轮 SSE 消费的结果。分类只看这里，不再回头翻流。

    计数自己数：`chat_stream` 不把解析器的 counts 暴露出来，而「零事件」判定要的正是
    text/thinking/tool/usage 四类的总数（`FinishEvent` 只是信息，不算事件）。
    """

    request_started: float | None = None
    text_events: int = 0
    thinking_events: int = 0
    tool_events: int = 0
    usage_events: int = 0
    tools: list[str] = field(default_factory=list)
    usage: UsageEvent | None = None
    saw_event: bool = False
    finish_reason: str | None = None
    relay_error: bool = False
    ask_user: bool = False
    questions: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    reason: str | None = None
    error: Exception | None = None

    @property
    def counted(self) -> int:
        return self.text_events + self.thinking_events + self.tool_events + self.usage_events


@dataclass(frozen=True)
class Verdict:
    """收尾判定：给 chat_logs 的状态、给 tasks 的状态、给用户的终稿。"""

    log_status: str
    task_status: str
    error_code: str | None
    error_message: str | None
    final_text: str


def part_kind(part: dict[str, Any]) -> str:
    """归一化 part 类别：带转写的语音等同文本，其余原样。

    企微网关按 spec §7.1 发 `AudioPart(type="audio")`，飞书侧历史上叫 `voice`：两个名字都认，
    否则「企微已经转好文字的语音」会被当成不支持的消息类型挡在门外。
    """
    kind = str(part.get("type") or "")
    if kind in ("voice", "audio") and part.get("transcript"):
        return "text"
    return kind


def message_type_of(parts: list[dict[str, Any]]) -> str:
    """chat_logs.message_type：全文本 text，单一非文本类型取该类型，混排 mixed。"""
    for part in parts:
        if part.get("type") == "quote":
            return f"quote_{part.get('kind') or 'text'}"
    others = sorted({part_kind(p) for p in parts} - {"text"})
    if not others:
        return "text"
    return others[0] if len(others) == 1 else "mixed"


def joined_text(parts: list[dict[str, Any]]) -> str:
    """把文本类 part 拼成一段；语音取转写。"""
    pieces = [
        str(p.get("text") or p.get("transcript") or "") for p in parts if part_kind(p) == "text"
    ]
    return "\n".join(p for p in pieces if p)


def strip_mention(text: str, bot_name: str) -> str:
    """去掉群里 @机器人 留下的名字，后面的命令匹配才认得出 `@机器人 stop`。"""
    if not bot_name:
        return text
    return re.sub(rf"@{re.escape(bot_name)}\s?", "", text)


async def next_event(gen: AsyncIterator[SseEvent]) -> SseEvent | None:
    """取下一个事件，流结束返回 None。

    包一层是为了让它成为一个普通协程：调用方要把它塞进 `asyncio.Task` 才能跨越「静默
    超时」继续活着，而 `StopAsyncIteration` 穿过 Task 边界的行为并不友好。
    """
    try:
        return await anext(gen)
    except StopAsyncIteration:
        return None


def log_entry(
    ctx: TaskContext,
    intake: Intake,
    *,
    status: str,
    relay_session_id: uuid.UUID | None = None,
    response_content: str | None = None,
    tools_used: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    usage: UsageEvent | None = None,
    content: BuiltContent | None = None,
) -> ChatLogEntry:
    """拼一条 chat_logs。耗时从认领时刻起算，`response_at` 只有成功才填。

    `content` 非空时消息四列以组装结果为准：入站 parts 只认得出「有张图」，组装完才知道
    真正发给模型的是哪句提示词、引用了什么、附件多大。
    """
    now = datetime.now(UTC)
    request_at = ctx.task.claimed_at or now
    speaker = intake.speaker
    return ChatLogEntry(
        bot_id=intake.bot.id,
        bot_key=intake.bot.bot_key,
        platform=intake.bot.platform,
        chat_type=intake.chat_type,
        message_type=content.message_type if content else intake.message_type,
        status=status,
        request_at=request_at,
        user_id=speaker.user_id,
        platform_user_id=speaker.platform_user_id,
        user_login=speaker.login_name,
        user_name=speaker.display_name,
        chat_id=intake.chat_id,
        session_key=intake.session_key,
        relay_session_id=relay_session_id,
        relay_server_id=intake.relay_id,
        model=intake.bot.model,
        stream_id=ctx.stream_id,
        task_id=ctx.task.id,
        message_content=content.text if content else intake.text,
        quoted_content=content.quoted_content if content else None,
        file_info=content.file_info if content else None,
        response_content=response_content,
        tools_used=list(tools_used or []),
        error_code=error_code,
        error_message=error_message,
        latency_ms=int((now - request_at).total_seconds() * 1000),
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cache_read_tokens=usage.cache_read_tokens if usage else None,
        cache_creation_tokens=usage.cache_creation_tokens if usage else None,
        cost_usd=None,
        response_at=now if status == "success" else None,
    )


class ChatTaskHandler:
    """spec §8.2 的主流水线。"""

    kind = "chat"
    SILENT_WARN_SECONDS = 30.0
    TICK_SECONDS = 1.0
    HEARTBEAT_SECONDS = 10.0
    LONG_TASK_SECONDS = 60
    QUEUED_NOTICE_SECONDS = 5.0
    SUPERSEDE_POLL_SECONDS = 0.1
    SUPERSEDE_POLLS = 200

    def __init__(self, *, timing: Timing = DEFAULT_TIMING) -> None:
        self.timing = timing
        self._on_first_event: Callable[[], None] | None = None  # 测试钩子

    async def run(self, ctx: TaskContext) -> None:
        beat = asyncio.create_task(self._heartbeat(ctx), name=f"chat-heartbeat-{ctx.task.id}")
        try:
            pre = await self._prepare(ctx)
            if pre is None:
                return
            outcome = await self._converse(ctx, pre)
        finally:
            beat.cancel()
            await asyncio.wait({beat})
        await self._finalize(ctx, pre, outcome)

    # ---- 前置 -------------------------------------------------------------

    async def _prepare(self, ctx: TaskContext) -> Prepared | None:
        started = ctx.clock()
        async with ctx.session_factory() as session:
            resolved = await self._resolve(session, ctx)
            await session.commit()
        if resolved is None:
            return None
        intake, relay, parts = resolved
        # supersede 必须先提交再等：被替代的任务跑在另一条连接上，看不见未提交的取消标记。
        async with ctx.session_factory() as session:
            victims = await tasks.supersede(
                session, intake.bot.id, intake.session_key, except_task_id=ctx.task.id
            )
            await session.commit()
        try:
            await self._wait_superseded(ctx, victims)
        except TimeoutError:
            async with ctx.session_factory() as session:
                await reply_once(
                    session,
                    ctx,
                    reply_context=intake.inbound.reply_context,
                    text="上一轮任务仍在停止中，请稍后继续。",
                )
                await tasks.finish(session, ctx.task.id, status="failed", error_code="session_busy")
                await session.commit()
            return None
        async with ctx.session_factory() as session:
            pre = await self._open(session, ctx, intake, relay, started)
            await session.commit()
        # 首帧必须等流建好并提交之后再写：flush 走的是另一个事务，看不见未提交的那一行。
        await pre.writer.flush(force=True)
        if self._needs_content():
            built = await self._build_content(ctx, pre, parts)
            if built is None:
                return None
            pre = replace(
                pre,
                content=built,
                request=replace(pre.request, user_content=self._user_content(built)),
            )
        ctx.log.info("task_started", bot_key=intake.bot.bot_key, chat_type=intake.chat_type)
        return pre

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        """这一轮从哪儿来。chat 是入站消息；子类可以换成别的入口（提交轮读状态快照）。"""
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

    async def _sessions(self, session: AsyncSession, ctx: TaskContext, intake: Intake) -> bool:
        """`sessions` 列表与 5 分钟内的序号切换（spec §8.2 步骤 4）；都不进 AI、不落 chat_logs。

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
        session_url = f"{relay.relay_url}/session/{info.relay_session_id}"
        if relay.runtime_node_id:
            from coreman.core.config import get_settings

            base = get_settings().public_base_url
            session_url = (
                f"{base}/api/admin/runtime-nodes/{relay.runtime_node_id}"
                f"/{relay.model_provider}/session/{info.relay_session_id}"
            )
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
        if info.is_new:
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
        return {}

    def _after_supervisor(self, pre: Prepared) -> None:
        """超时看护器建好之后的挂钩。默认什么都不做（两阶段超时照常从头计时）。"""

    @staticmethod
    def _user_content(built: BuiltContent) -> str | list[dict[str, Any]]:
        """纯文本保持字符串（与 M2 请求体一致），含媒体才用 content parts。"""
        if all(p.get("type") == "text" for p in built.parts):
            return sanitize_user_input("\n".join(str(p["text"]) for p in built.parts))
        return sanitize_parts(built.parts)

    async def _build_content(
        self, ctx: TaskContext, pre: Prepared, parts: list[dict[str, Any]]
    ) -> BuiltContent | None:
        """开流之后再下载：提示行要写进思考区给用户看，流不存在就没处写。

        返回 None = 这一轮到此为止（已经回过用户、结过任务、落过日志）。媒体下载失败记
        失败任务；「暂不支持」与「语音没转写出来」沿用 M2：答过了就算这轮答完了。
        """
        fetcher: MediaFetcher
        owns_fetcher = pre.intake.bot.platform == "feishu" or ctx.media_fetcher is None
        if pre.intake.bot.platform == "feishu":
            from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
            from coreman.core.platforms.feishu import FeishuClient
            from coreman.core.platforms.feishu_media import FeishuMediaFetcher

            credentials = decrypt_json(ctx.cipher, pre.intake.bot.credentials_enc, CREDENTIALS_AAD)
            fetcher = FeishuMediaFetcher(
                FeishuClient(credentials.get("app_id", ""), credentials.get("app_secret", "")),
                message_id=pre.intake.inbound.platform_msg_id,
            )
        else:
            fetcher = ctx.media_fetcher or MediaFetcher()

        async def on_hint(text: str) -> None:
            # 一有提示就刷出去：攒到 build 之后再写，用户在下载那十几秒里只能看到空白。
            pre.writer.set_thinking_line(text)
            await pre.writer.flush(force=True)

        try:
            builder = ContentBuilder(
                fetcher, locale=ctx.locale, platform=pre.intake.bot.platform, on_hint=on_hint
            )
            build_task = asyncio.create_task(builder.build(parts, text=pre.intake.text))
            stopping = asyncio.create_task(ctx.cancel_event.wait())
            try:
                while True:
                    ready, _ = await asyncio.wait(
                        {build_task, stopping},
                        timeout=self.TICK_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    reason = (
                        (ctx.cancel_reason or "cancelled") if ctx.cancel_event.is_set() else None
                    )
                    if not reason and await pre.supervisor.tick(ctx.clock()) == "expired":
                        reason = "hard_ttl"
                    if reason:
                        build_task.cancel()
                        await asyncio.gather(build_task, return_exceptions=True)
                        await self._finalize(ctx, pre, Outcome(cancelled=True, reason=reason))
                        return None
                    if build_task in ready:
                        built = build_task.result()
                        break
            finally:
                build_task.cancel()
                stopping.cancel()
                await asyncio.gather(build_task, stopping, return_exceptions=True)
        finally:
            # 自己临时建的那份自己收；WorkerService 传下来的那份归它管，不能在这里关。
            if owns_fetcher:
                await fetcher.aclose()
        if built.failed is None:
            return built
        ctx.log.info("content_rejected", code=built.failed_code, message_type=built.message_type)
        failed = built.failed_code == "media_failed"
        # 凑一份与 `_finalize` 同构的判定，收尾三件事（抢终态、送终稿、落日志）才好照它的
        # 规矩来：媒体下载失败记失败任务，「暂不支持」与「语音没转写出来」沿用 M2 的口径。
        verdict = Verdict(
            "error" if failed else "success",
            "failed" if failed else "succeeded",
            "media_failed" if failed else None,
            built.failed if failed else None,
            built.failed,
        )
        async with ctx.session_factory() as session:
            owned = await tasks.finish(
                session,
                ctx.task.id,
                status=verdict.task_status,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                only_active=True,
            )
            await session.commit()
        if not owned:
            # reaper 已经替这一轮收过尾、也告诉过用户了，再改终稿、再补日志只会自相矛盾。
            ctx.log.warning("task_already_finalized", status=verdict.task_status)
            return None
        pre.writer.thinking.add_end(msg("thinking_end", ctx.locale))
        done = await pre.writer.complete(verdict.final_text)
        # 下载几十兆要十几秒，这期间网关多半已经排空过这条流：那时没人再跟流，终稿得自己推。
        await self._push_if_proactive(ctx, pre, verdict, done, plain=True)
        ctx.chat_logs.submit(
            log_entry(
                ctx,
                pre.intake,
                status=verdict.log_status,
                relay_session_id=pre.info.relay_session_id,
                response_content=verdict.final_text,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                content=built,
            )
        )
        return None

    def _queued_seconds(self, ctx: TaskContext) -> int | None:
        """排队超过 5 秒才提示——1 秒的兜底轮询本来就会带来零点几秒的延迟。"""
        claimed, run_after = ctx.task.claimed_at, ctx.task.run_after
        if claimed is None or run_after is None:
            return None
        waited = (claimed - run_after).total_seconds()
        return int(waited) if waited > self.QUEUED_NOTICE_SECONDS else None

    # ---- 消费 -------------------------------------------------------------

    async def _converse(self, ctx: TaskContext, pre: Prepared) -> Outcome:
        out = Outcome(request_started=ctx.clock())
        client = ctx.relay_client_factory(pre.relay)
        # 显式关闭生成器才能断开连接，让 relay 停止当前 CLI。
        gen = client.chat_stream(
            pre.request, total_timeout=float(pre.intake.bot.sse_timeout_seconds)
        )
        supervisor = pre.supervisor
        stopping = asyncio.ensure_future(ctx.cancel_event.wait())
        pending: asyncio.Task[SseEvent | None] | None = None
        silent_since = ctx.clock()
        try:
            while True:
                if ctx.cancel_event.is_set():
                    out.cancelled, out.reason = True, ctx.cancel_reason
                    return out
                if pending is None:
                    pending = asyncio.ensure_future(next_event(gen))
                # 1 秒一跳：超时判定不能依赖「下一帧什么时候来」，沉默的流也要按点切后台。
                done, _ = await asyncio.wait(
                    {pending, stopping},
                    timeout=self.TICK_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                now = ctx.clock()
                if await supervisor.tick(now) == "expired":
                    # 硬 TTL：返回即可，finally 会断连（=让 relay 杀掉这一轮 CLI）。
                    out.cancelled, out.reason = True, "hard_ttl"
                    return out
                if pending in done:
                    event, pending = pending.result(), None
                    if event is None:
                        return out
                    silent_since = now
                    self._apply(ctx, pre, out, event)
                    await pre.writer.flush()
                elif not done and now - silent_since >= self.SILENT_WARN_SECONDS:
                    # 静默只提醒、不中断：工具跑十几分钟不吐字是正常的，中断才是事故。
                    ctx.log.warning("sse_silent", seconds=int(now - silent_since))
                    silent_since = now
                await supervisor.on_progress(now, pre.writer.pending_text, pre.writer.boundaries)
                if out.ask_user:
                    return out
        except Exception as exc:  # noqa: BLE001 任何异常都只算这一轮失败，统一进 _classify
            ctx.log.warning("task_failed", error=type(exc).__name__)
            out.error = exc
            return out
        finally:
            stopping.cancel()
            if pending is not None:
                # 先把还在等下一帧的那个 Task 停掉，生成器才关得动；关掉 = 断开 = 让 relay 停任务。
                pending.cancel()
                await asyncio.wait({pending})
            await gen.aclose()
            await client.aclose()

    def _apply(self, ctx: TaskContext, pre: Prepared, out: Outcome, event: SseEvent) -> None:
        if not out.saw_event:
            # 单独记一个「见过帧」的标志：`counted` 不数 FinishEvent，用它判首帧会重复触发。
            out.saw_event = True
            from coreman.core.observability.metrics import FIRST_BYTE

            if out.request_started is not None:
                FIRST_BYTE.labels(str(pre.relay.id)).observe(
                    max(0, ctx.clock() - out.request_started)
                )
            ctx.log.info("first_event", event_type=type(event).__name__)
            if self._on_first_event is not None:
                self._on_first_event()
        writer = pre.writer
        if isinstance(event, TextDelta):
            out.text_events += 1
            writer.add_text(event.text)
        elif isinstance(event, ThinkingDelta):
            out.thinking_events += 1
            writer.add_thinking(event.text)
        elif isinstance(event, ToolUseStart):
            out.tool_events += 1
            out.tools.append(event.name)
            writer.add_tool(event.name)
        elif isinstance(event, RelayErrorEvent):
            out.relay_error = True
            writer.add_text(event.text)
        elif isinstance(event, UsageEvent):
            out.usage_events += 1
            out.usage = event
        elif isinstance(event, AskUserQuestionEvent):
            out.ask_user, out.questions = True, list(event.questions)
        elif isinstance(event, FinishEvent):
            out.finish_reason = event.reason
        # Keep consuming usage and questions following the terminal marker.

    # ---- 收尾 -------------------------------------------------------------

    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        """spec §8.4 的流结束分类。"""
        locale, text, url = ctx.locale, pre.writer.pending_text, pre.session_url
        if out.cancelled:
            return self._cancelled(ctx, text, out.reason)
        if out.error is not None:
            name = type(out.error).__name__
            return Verdict(
                "error",
                "failed",
                name,
                f"{name}: {out.error}",
                msg("relay_error", locale, relay=pre.relay.name),
            )
        if out.ask_user:
            if not out.questions:
                # 问卷解析出来是空的：这一轮什么也问不出来，按失败收尾让用户换个说法。
                return Verdict(
                    "error",
                    "failed",
                    "ask_user_invalid",
                    "AskUserQuestion 参数无效",
                    text
                    + msg("ask_user_invalid", locale)
                    + msg("session_link_suffix", locale, url=url),
                )
            brief = question_brief(
                out.questions[0], index=0, total=len(out.questions), locale=locale
            )
            # 终稿带上首题的完整说明：卡片上的选项被截到 11 个字，只看卡片选不明白。
            return Verdict(
                "ask_user", "succeeded", None, None, f"{text}\n\n{brief}" if text else brief
            )
        if out.relay_error:
            return Verdict(
                "error",
                "failed",
                "x_relay_error",
                "运行时以正文回传了错误",
                text or msg("relay_error_text", locale),
            )
        if out.counted == 0:
            return Verdict(
                "error",
                "failed",
                "empty_stream",
                "运行时未返回任何事件",
                msg("empty_stream", locale, url=url),
            )
        if out.finish_reason not in {"stop", "end_turn", "completed"}:
            return Verdict(
                "error",
                "failed",
                "incomplete_result",
                "未确认执行完成",
                msg("relay_error", locale, relay=pre.relay.name),
            )
        if out.text_events == 0:
            body = (
                msg("no_text_with_tools", locale, n=out.tool_events, url=url)
                if out.tool_events
                else self._empty_success_text(ctx)
            )
            return Verdict("success", "succeeded", None, None, text + body)
        return Verdict("success", "succeeded", None, None, text + msg("done_suffix", locale))

    def _empty_success_text(self, ctx: TaskContext) -> str:
        """一个字也没说、一个工具也没调，却是正常收尾时给用户的交代。"""
        return msg("no_text_no_tools", ctx.locale)

    def _cancelled(self, ctx: TaskContext, text: str, reason: str | None) -> Verdict:
        if reason == "user_stop":
            return Verdict(
                "stopped",
                "cancelled",
                "user_stop",
                "用户主动停止",
                text + msg("task_stopped_suffix", ctx.locale),
            )
        if reason == "superseded":
            return Verdict(
                "stopped",
                "cancelled",
                "superseded",
                "已被新消息替代",
                text + msg("superseded_suffix", ctx.locale),
            )
        return Verdict(
            "timeout",
            # 硬 TTL 是平台主动掐的超时，任务态要与「用户取消」分开，运营才看得出差别。
            "timed_out" if reason == "hard_ttl" else "cancelled",
            reason or "cancelled",
            "任务被取消（超时）",
            text or msg("worker_lost", ctx.locale),
        )

    def _with_note(self, ctx: TaskContext, text: str, note: str) -> str:
        """把额度表 / 预警接在终稿后面，但让「✅ 任务已完成」继续压尾。

        `_classify` 已经把 ✅ 拼进终稿了，直接追加会变成「✅ 之后还有一大张表」，读起来像
        任务完成之后又出了什么事；剥掉再拼回去，顺序就与旧实现的「表/预警 → ✅」一致。
        """
        done = msg("done_suffix", ctx.locale)
        if text.endswith(done):
            return text[: -len(done)] + note + done
        return text + note

    async def _postprocess_rate_limit(
        self, ctx: TaskContext, pre: Prepared, verdict: Verdict
    ) -> tuple[Verdict, dict[str, Any] | None]:
        """spec §8.8：触限附额度表、未触限附预警、管理员再推一张切换卡。只在有终稿的收尾做。

        取消 / 超时那几路不进来：用户根本没拿到模型的回答，一张额度表帮不上任何忙。
        """
        if verdict.log_status not in ("success", "error") or not verdict.final_text:
            return verdict, None
        now = datetime.now(UTC)
        limited = is_rate_limited(verdict.final_text)
        async with ctx.session_factory() as session:
            relays = list(
                (
                    await session.execute(
                        select(RelayServer)
                        .where(RelayServer.is_active.is_(True))
                        .order_by(RelayServer.name)
                    )
                ).scalars()
            )
            current = next((r for r in relays if r.id == pre.relay.id), None)
            if current is None:
                # 这一轮跑完的工夫实例被下线了：额度数据已经无从谈起，终稿原样交付。
                return verdict, None
            note = (
                quota_table(relays, current_id=current.id, now=now, locale=ctx.locale)
                if limited
                else quota_warning(current, now, ctx.locale)
            )
            if note:
                verdict = replace(
                    verdict, final_text=self._with_note(ctx, verdict.final_text, note)
                )
            if not limited or pre.intake.speaker.user_id is None:
                return verdict, None
            user = await session.get(User, pre.intake.speaker.user_id)
            member_ids = set(
                (
                    await session.execute(
                        select(BotMember.user_id).where(BotMember.bot_id == pre.intake.bot.id)
                    )
                )
                .scalars()
                .all()
            )
            # 切换是写操作：认不出人、或者这个人本来就没权限，就只给表不给卡。
            if user is None or not can_switch_relay(user, pre.intake.bot, member_ids):
                return verdict, None
            creator = await session.get(User, pre.intake.bot.created_by)
            target = pick_idle_server(
                relays,
                current=current,
                bot=pre.intake.bot,
                user=user,
                creator_team_id=creator.team_id if creator else None,
            )
            if target is None:
                return verdict, None
            prefix = make_ratelimit_prefix(
                pre.intake.bot.bot_key, pre.intake.speaker.platform_user_id, now=time.time()
            )
            pct = (
                float(target.rate_limit_7d_used_pct)
                if target.rate_limit_7d_used_pct is not None
                else None
            )
            state = {
                "idx": 0,
                "platform_user_id": pre.intake.speaker.platform_user_id,
                "user_id": str(user.id),
                "current_relay_id": str(current.id),
                "current_name": current.name,
                "target_relay_id": str(target.id),
                "target_name": target.name,
                "target_pct_7d": pct,
                "chat_id": pre.intake.chat_id,
                "chat_type": pre.intake.chat_type,
            }
            await interactions.open_state(
                session,
                bot_id=pre.intake.bot.id,
                kind=interactions.KIND_RELAY_SWITCH,
                scope_key=interactions.choice_scope(
                    pre.intake.bot.id, pre.intake.speaker.platform_user_id
                ),
                state=state,
                task_id_prefix=prefix,
                # 30 分钟后作废：额度数据早就换过几轮，那时候再按这张卡切是照着陈数据切。
                expires_at=now + timedelta(minutes=30),
            )
            await session.commit()
        ctx.log.info("relay_switch_offered", target=target.name, prefix=prefix)
        return verdict, switch_offer_card(
            task_id=question_task_id(prefix, 0),
            current_name=current.name,
            target_name=target.name,
            pct_text=f"{pct:.0f}%" if pct is not None else msg("rl_pct_unknown", ctx.locale),
            icon_url=await self._icon_url(ctx),
            locale=ctx.locale,
        )

    async def _finalize(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> None:
        intake = pre.intake
        verdict = self._classify(ctx, pre, out)
        pre.writer.thinking.add_end(msg("thinking_end", ctx.locale))
        supervisor = pre.supervisor
        elapsed = int(ctx.clock() - pre.started_clock)
        # 收尾的第一件事是抢终态：守卫没过说明 reaper 已经替这个任务写过终态、也已经告诉过
        # 用户「异常中断」。那时候再推送、再改写终稿、再补一条 chat_log，用户会收到两份自相
        # 矛盾的回答，管理台也会多一条假成功记录——所以就此收手，一个字都不再落。
        async with ctx.session_factory() as session:
            owned = await tasks.finish(
                session,
                ctx.task.id,
                status=verdict.task_status,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                only_active=True,
            )
            await session.commit()
        if not owned:
            ctx.log.warning("task_already_finalized", status=verdict.task_status, elapsed_s=elapsed)
            return
        verdict, offer = await self._postprocess_rate_limit(ctx, pre, verdict)
        # 待答状态与卡片排在抢到终态之后：被 reaper 收过尾的那一轮已经告诉用户「异常中断」，
        # 再开一轮提问就是让用户对着一张没人接的卡片作答。
        card = (
            await self._open_choice(ctx, pre, out.questions)
            if verdict.log_status == "ask_user"
            else None
        )
        # 触限时模型不会同时提问，两张卡不会真的打架；真撞上了以切换卡为准——额度没了，
        # 那一轮问答无论怎么答都跑不起来。
        card = offer or card
        if out.reason == "hard_ttl":
            # 硬 TTL 是「被掐掉」，不是完成：只发终止通知，不发 ✅ 完成推送。
            await supervisor.on_expired()
            await pre.writer.complete(verdict.final_text, pending_card=card)
        elif supervisor.switched:
            # 只有真成功才配 ✅：出错 / 超时 / 被停都走不冠完成前缀的那条路。
            await supervisor.on_finish(
                verdict.final_text,
                success=verdict.log_status == "success",
                waiting=verdict.log_status == "ask_user",
            )
            await pre.writer.complete(verdict.final_text, pending_card=card)
            # 网关早就不跟这条流了，它那条「finish 之后发卡片」的路不会再走：卡片得自己送。
            await self._queue_card(ctx, pre, card)
        else:
            # 先 complete 再看它带回来的投递状态：这样「网关翻 proactive」与「worker 收尾」
            # 无论谁先，终稿都只由后手那一边负责送达，不会两边都以为对方会送。
            done = await pre.writer.complete(verdict.final_text, pending_card=card)
            await self._push_if_proactive(ctx, pre, verdict, done, card)
        # The delivered final response already carries completion; do not race it
        # with a separate notification or label a waiting-user turn as complete.
        ctx.chat_logs.submit(
            log_entry(
                ctx,
                intake,
                status=verdict.log_status,
                relay_session_id=pre.info.relay_session_id,
                response_content=verdict.final_text,
                tools_used=out.tools,
                error_code=verdict.error_code,
                error_message=verdict.error_message,
                usage=out.usage,
                content=pre.content,
            )
        )
        ctx.log.info(
            "task_finished",
            status=verdict.log_status,
            elapsed_s=elapsed,
            events=out.counted,
            tools=len(out.tools),
        )
        await self._after_finalize(ctx, pre, verdict)

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        """收尾之后的挂钩（清理本轮独有的状态）。只在这一轮真的抢到终态时才会走到。"""

    @staticmethod
    async def _icon_url(ctx: TaskContext) -> str:
        """卡片左上角的小图标；没配就整个字段不发（见 cards._source）。"""
        return str(await ctx.settings_store.get("card_icon_url", default="") or "")

    async def _open_choice(
        self, ctx: TaskContext, pre: Prepared, questions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """写 interaction_states(choice) 并渲染首题卡片；同一发言者的旧状态被覆盖。

        `context` 是提交轮重建这次请求要的全部东西（relay 会话、模型、工作目录、提示词）：
        答完题再回到模型时，原来那个任务早就结束了，只有这份快照能把上下文接回去。里面
        一个密钥都不能有——env 与凭据留在 bots 表里，提交轮自己去解。
        """
        intake = pre.intake
        prefix = make_choice_prefix(
            intake.bot.bot_key,
            intake.speaker.platform_user_id,
            now=time.time(),
            rnd=uuid.uuid4().hex,
        )
        context = {
            "relay_session_id": str(pre.info.relay_session_id),
            "stream_id": ctx.stream_id,
            "relay_server_id": str(pre.relay.id),
            "model": intake.bot.model,
            "working_dir": intake.bot.working_dir,
            "backend": pre.request.backend,
            "system_prompt": pre.request.system_prompt,
            "chat_type": intake.chat_type,
            "chat_id": intake.chat_id,
            "session_key": intake.session_key,
            "user_id": str(intake.speaker.user_id) if intake.speaker.user_id else None,
            "platform_user_id": intake.speaker.platform_user_id,
            "sse_timeout_seconds": intake.bot.sse_timeout_seconds,
            "verbosity_level": intake.bot.verbosity_level,
            "effort_level": intake.bot.effort_level,
        }
        state = {
            "questions": questions,
            "answers": [],
            "current_index": 0,
            "waiting_for_text": False,
            "waiting_since": None,
            "context": context,
        }
        async with ctx.session_factory() as session:
            await interactions.open_state(
                session,
                bot_id=intake.bot.id,
                kind=interactions.KIND_CHOICE,
                scope_key=interactions.choice_scope(intake.bot.id, intake.speaker.platform_user_id),
                state=state,
                task_id_prefix=prefix,
            )
            await session.commit()
        ctx.log.info("ask_user_opened", questions=len(questions), prefix=prefix)
        return choice_card(
            questions[0],
            index=0,
            total=len(questions),
            task_id=question_task_id(prefix, 0),
            icon_url=await self._icon_url(ctx),
            locale=ctx.locale,
        )

    async def _queue_card(
        self, ctx: TaskContext, pre: Prepared, card: dict[str, Any] | None
    ) -> None:
        """主动模式下卡片也得自己入队；幂等键与网关那一路相同，两边抢着入也只会发一张。"""
        if card is None:
            return
        async with ctx.session_factory() as session:
            await outbox.add(
                session,
                bot_id=pre.intake.bot.id,
                platform=pre.intake.bot.platform,
                kind="send",
                dedupe_key=f"{ctx.task.id}:card:0",
                target={"chat_id": pre.intake.chat_id},
                payload={"card": card},
            )
            await session.commit()

    async def _push_if_proactive(
        self,
        ctx: TaskContext,
        pre: Prepared,
        verdict: Verdict,
        done: streams.Completion,
        card: dict[str, Any] | None = None,
        *,
        plain: bool = False,
    ) -> None:
        """流被别人改成了 proactive（网关排空、接管、租约换代）：终稿没人推了，自己推。

        判据用收尾那条 UPDATE 带回来的状态，不再另开一个事务去读：读和写之间正好插进一次
        drain，就会两边都以为对方负责终稿。`offset` 是网关已经投递出去的前缀长度（排空的
        finish 帧带走了那一段），只推剩下的，免得用户把同一段话看两遍。

        本任务自己切的后台走 `on_finish`；这里只兜「不是我切的」那一种，dedupe_key 固定
        `:send:final`，与后台增量的序号键不冲突，重复收尾也只会入队一条。

        只有真成功才配 ✅：出错 / 超时 / 被停走 `finish_failed`（同 `on_finish` 的口径），
        否则被接管的失败任务会被说成「任务已完成」。
        """
        if done.delivery_mode != "proactive":
            return
        pusher = pre.supervisor.pusher
        rest = (
            verdict.final_text[done.offset :]
            if verdict.log_status == "success" and not plain
            else verdict.final_text
        )
        ctx.log.info(
            "proactive_final_push",
            stream_version=done.version,
            status=verdict.log_status,
            offset=done.offset,
        )
        if verdict.log_status == "success" and not plain:
            text = pusher.cap(msg("bg_done_prefix", ctx.locale) + rest)
        else:
            (text,) = pusher.finish_failed(rest)
        await pre.supervisor.push(text, dedupe_key=f"{ctx.task.id}:send:final")
        # 卡片排在终稿之后：先正文后选项卡，顺序反了用户会对着一张没头没尾的卡片发愣。
        await self._queue_card(ctx, pre, card)

    async def _heartbeat(self, ctx: TaskContext) -> None:
        """10 秒一次：既是「我还活着」的证明，也是取消信号的兜底传导路径。"""
        while True:
            await asyncio.sleep(self.HEARTBEAT_SECONDS)
            try:
                await ctx.heartbeat()
            except Exception:  # noqa: BLE001 心跳写不进去下一轮再来，不能把任务带走
                ctx.log.warning("task_heartbeat_failed")
