"""choice_submit 处理器：把用户对 AskUserQuestion 的回答作为新 user 消息，用同一会话续跑。

与普通 chat 的差别只有三处：入口不是入站消息而是 interaction_states 的快照；流从创建就是
proactive（没有 req_id 可用，全部经 outbox 主动推送，带静默心跳）；落库 message_type 为
ask_user_answer。其余（SSE 消费、分类、嵌套提问、后台推送）全部复用 ChatTaskHandler。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import interactions, sessions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, InteractionState, RelayServer
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import format_answers
from coreman.runtime.bus import outbox, tasks
from coreman.runtime.worker.background import DEFAULT_TIMING, Timing
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Intake, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound

# 提交轮的静默心跳节奏：1.5 / 4 / 7 / 10 分钟各报一次「还在跑」。
SUBMIT_SILENCE_MARKS = (90.0, 240.0, 420.0, 600.0)


@dataclass(frozen=True)
class _Snapshot:
    """提交轮要接回去的那份上下文：待答状态行的 id，与提问轮存下的 `context`。"""

    state_id: uuid.UUID
    context: dict[str, Any]


class ChoiceSubmitHandler(ChatTaskHandler):
    """答完一轮问卷之后把答案送回模型的那一轮（spec §8.5）。"""

    kind = "choice_submit"

    def __init__(self, *, timing: Timing = DEFAULT_TIMING) -> None:
        if not timing.silence_marks:
            timing = replace(timing, silence_marks=SUBMIT_SILENCE_MARKS)
        super().__init__(timing=timing)
        # WorkerService 把一个处理器实例复用给所有并发任务，所以快照只能按 task_id 分开存：
        # 挂成 self._state 的话，两个人同时答完题就会互相串会话、互相删对方的新状态。
        self._snapshots: dict[int, _Snapshot] = {}

    async def run(self, ctx: TaskContext) -> None:
        try:
            await super().run(ctx)
        finally:
            # 无论怎么退场（就地了结、被取消、处理器抛异常）都要把这一份收掉，不留常驻内存。
            self._snapshots.pop(ctx.task.id, None)

    def _snapshot(self, ctx: TaskContext) -> _Snapshot:
        """`_resolve` 放行之后才有值；放行之前的分支都直接 return 了，取不到就是代码错了。"""
        return self._snapshots[ctx.task.id]

    # ---- 入口：状态快照而不是入站消息 -------------------------------------

    async def _resolve(
        self, session: AsyncSession, ctx: TaskContext
    ) -> tuple[Intake, RelayServer, list[dict[str, Any]]] | None:
        """从 interaction_states 的快照重建这一轮；能就地了结的分支在这里全部了结。"""
        bot = await session.get(Bot, ctx.task.bot_id)
        state_id = uuid.UUID(str(ctx.task.payload.get("state_id")))
        st = await session.get(InteractionState, state_id, populate_existing=True)
        chat_id = str(ctx.task.payload.get("chat_id") or "")
        if bot is None or not bot.enabled or st is None:
            # 状态已经被 reset / 取消清掉了：那一轮问卷不作数，这条提交任务也就没有归宿。
            ctx.log.warning("submit_state_missing", state_id=str(state_id))
            await tasks.finish(session, ctx.task.id, status="cancelled", error_code="state_missing")
            return None
        if not await interactions.set_status(session, st.id, "submitted"):
            # 抢不到 open→submitted 就是有人先提交过了：连点两下卡片只该跑一轮。
            if st.status == "submitted":
                await self._say(
                    session,
                    bot,
                    chat_id,
                    f"{ctx.task.id}:send:duplicate",
                    msg("choice_duplicate", ctx.locale),
                )
            ctx.log.info("submit_duplicate", state_id=str(st.id), status=st.status)
            await tasks.finish(
                session, ctx.task.id, status="succeeded", result={"submit": "duplicate"}
            )
            return None
        context = dict(st.state.get("context") or {})
        relay = (
            await session.get(RelayServer, uuid.UUID(str(context.get("relay_server_id"))))
            if context.get("relay_server_id")
            else None
        )
        if relay is None or not relay.is_active or bot.relay_server_id != relay.id:
            # relay 换了（或停用了）：快照里的会话 id 在新实例上根本不存在，续不下去。
            await interactions.remove(session, st.id)
            ctx.log.warning("submit_config_changed", state_id=str(st.id))
            await self._say(
                session,
                bot,
                chat_id,
                f"{ctx.task.id}:send:config_changed",
                msg("choice_config_changed", ctx.locale),
            )
            await tasks.finish(
                session, ctx.task.id, status="succeeded", result={"submit": "config_changed"}
            )
            return None
        self._snapshots[ctx.task.id] = _Snapshot(st.id, context)
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=str(context.get("platform_user_id") or ""),
            resolver=ctx.openuserid,
        )
        text = format_answers(
            list(st.state.get("questions") or []),
            list(st.state.get("answers") or []),
            ctx.locale,
        )
        # 提交任务总带着触发它的入站事件（文本答案或卡片事件）：这里只用它填 Intake，
        # 不碰它的 req_id——那条 stream 早就被提问轮的终稿收掉了。
        inbound = await load_inbound(session, ctx.task)
        intake = Intake(
            bot,
            relay,
            inbound,
            speaker,
            chat_id,
            str(context.get("chat_type") or "single"),
            str(context.get("session_key") or chat_id),
            text,
            "ask_user_answer",
        )
        return intake, relay, [{"type": "text", "text": text}]

    async def _say(
        self, session: AsyncSession, bot: Bot, chat_id: str, key: str, markdown: str
    ) -> None:
        """就地了结的那几条只能走 outbox：这一轮还没有流，也不该为一句话建一条。"""
        await outbox.add(
            session,
            bot_id=bot.id,
            platform=bot.platform,
            kind="send",
            dedupe_key=key,
            target={"chat_id": chat_id},
            payload={"markdown": markdown},
        )

    # ---- 准备阶段的几处差异 -----------------------------------------------

    async def _session_info(
        self, session: AsyncSession, ctx: TaskContext, intake: Intake, backend: str
    ) -> sessions.SessionInfo:
        """续跑快照里的那条 relay 会话，不碰 chat_sessions——这一轮不是新的用户回合。"""
        return sessions.SessionInfo(
            relay_session_id=uuid.UUID(str(self._snapshot(ctx).context["relay_session_id"])),
            is_new=False,
            speaker_changed=False,
        )

    # 续跑仍使用原 relay 会话，但身份、系统授权和提示词按本轮重新构建。
    # 不复用快照里的 SYS_USER 与业务系统授权，避免人员停用或撤权后继续带旧身份。

    def _stream_kwargs(self, ctx: TaskContext, intake: Intake) -> dict[str, Any]:
        """这条流从创建就是主动模式：没有 req_id，企微那边推不了，全部走 outbox。

        `stream_id` 沿用首次提问轮，只保留一个 `|submit` 后缀，嵌套提问不无限累加。
        """
        root_stream = str(self._snapshot(ctx).context.get("stream_id") or "").split("|submit", 1)[0]
        return {
            "stream_id": f"{root_stream}|submit",
            "delivery_mode": "proactive",
            "background_state": {
                "mode": "proactive",
                "offset": 0,
                "finish_suffix": "",
                "switched_at": datetime.now(UTC).isoformat(),
            },
            "reply_context": {
                "gateway_instance": ctx.instance_id,
                "received_at": datetime.now(UTC).isoformat(),
                "chat_type": intake.chat_type,
                "chat_id": intake.chat_id,
            },
        }

    def _after_supervisor(self, pre: Prepared) -> None:
        pre.supervisor.start_proactive(pre.started_clock)

    def _needs_content(self) -> bool:
        """答案已经是成品文本（`format_answers`），没有媒体要下载。"""
        return False

    def _empty_success_text(self, ctx: TaskContext) -> str:
        """模型收下答案后一声不吭也算办完了：别拿「换个说法重发」去催一个已经答过题的人。"""
        return msg("choice_submit_done", ctx.locale)

    # ---- 收尾：这轮问卷到此为止 -------------------------------------------

    async def _after_finalize(self, ctx: TaskContext, pre: Prepared, verdict: Verdict) -> None:
        """删掉这一轮的待答状态。

        嵌套提问已经在 `_open_choice` 里用 `open_state` 覆盖了同作用域的旧行，那时这个 id
        已经不在库里了，这次 `remove` 就是一次无操作——绝不能反过来把新状态删掉。
        """
        snap = self._snapshots.get(ctx.task.id)
        if snap is None:
            return
        async with ctx.session_factory() as session:
            await interactions.remove(session, snap.state_id)
            await session.commit()
