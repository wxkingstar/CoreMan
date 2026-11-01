"""群聊旁听的判断任务：群里安静下来后，问判断模型这位 AI 员工该不该开口。

说多说少交给模型；代码只管三件事：不和正在进行的回复重叠、尊重静音、机器人之间连着说了
太多（防误判导致的无限接话）就停。判断为该说话、且开关是 on 时，排一个插话对话回合；
shadow 只记录。每次判断（含跳过）都写进 `group_observe_decisions`，管理台据此复核。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.chat import decider as decider_mod
from coreman.core.chat import group_observe
from coreman.core.config import get_settings
from coreman.core.db.models import Bot, GroupObserveDecision, GroupObserveState, InboundEvent, Task
from coreman.runtime.worker.context import TaskContext

BUSY_RETRY_SECONDS = 5.0
# 一直有回复在跑（例如长任务）时最多重排这么多次，之后放弃这一批，等下一条消息。
BUSY_MAX_ATTEMPTS = 60


class GroupObserveHandler:
    kind = "group_observe"

    def __init__(self, decider: decider_mod.Decider | None = None) -> None:
        self._decider = decider
        self._loaded = decider is not None

    def _get_decider(self) -> decider_mod.Decider | None:
        if not self._loaded:
            self._decider = decider_mod.from_settings(get_settings())
            self._loaded = True
        return self._decider

    async def run(self, ctx: TaskContext) -> None:
        chat_id = str(ctx.task.payload.get("chat_id") or "")
        event_id = int(ctx.task.payload.get("event_id") or 0)
        store = ctx.settings_store
        quiet = float(await store.get("group_observe_quiet_seconds", default=8))
        max_wait = float(await store.get("group_observe_max_wait_seconds", default=60))
        threshold = float(await store.get("group_observe_threshold", default=0.3))
        streak_limit = int(await store.get("group_observe_bot_streak_limit", default=6))
        now = datetime.now(UTC)
        async with ctx.session_factory() as session:
            bot = await session.get(Bot, ctx.task.bot_id)
            if bot is None or not bot.enabled or not group_observe.observing(bot):
                await self._finish(session, ctx, "cancelled", "observe_off")
                return
            mode = bot.group_observe
            state = await session.get(GroupObserveState, (bot.id, chat_id), with_for_update=True)
            if state is None or state.pending_since is None:
                # 已被更早的任务一起判断过，或期间有人 @ 了它。
                await self._finish(session, ctx, "succeeded", None, {"skipped": "nothing_pending"})
                return
            waited = (now - state.pending_since).total_seconds()
            idle = (now - (state.last_message_at or now)).total_seconds()
            latest = state.last_event_id == event_id
            if not latest and waited < max_wait:
                await self._finish(session, ctx, "succeeded", None, {"skipped": "newer_message"})
                return
            if latest and idle < quiet and waited < max_wait:
                await tasks.defer(
                    session, ctx.task.id, seconds=min(quiet - idle, max_wait - waited)
                )
                await session.commit()
                return
            if await self._busy(session, bot.id, chat_id):
                # 正在回复：等它说完再看，待判断保留（更新的消息由它自己的任务等）。
                if not latest:
                    await self._finish(
                        session, ctx, "succeeded", None, {"skipped": "newer_message"}
                    )
                    return
                if ctx.task.attempts < BUSY_MAX_ATTEMPTS:
                    await tasks.defer(session, ctx.task.id, seconds=BUSY_RETRY_SECONDS)
                    await session.commit()
                    return
                state.pending_since = None
                await self._record(session, bot, chat_id, event_id, mode, "busy")
                await self._finish(session, ctx, "succeeded", None, {"skipped": "busy"})
                return
            # 这一批由本任务判断：之后来的消息重新开始攒。
            state.pending_since = None
            muted = state.muted_until is not None and state.muted_until > now
            streak = state.bot_streak
            if muted or streak >= streak_limit:
                outcome = "muted" if muted else "bot_streak"
                await self._record(session, bot, chat_id, event_id, mode, outcome)
                await self._finish(session, ctx, "succeeded", None, {"skipped": outcome})
                return
            lines = await group_observe.transcript(session, bot, chat_id, now=now)
            await session.commit()
        if not lines or lines[-1].sender_kind == "self":
            async with ctx.session_factory() as session:
                await self._finish(session, ctx, "succeeded", None, {"skipped": "self_last"})
            return
        decider = self._get_decider()
        payload: dict[str, Any] = {
            "bot": {"name": bot.name, "role": bot.group_observe_scope or bot.description},
            "recent_messages": [line.out() for line in lines],
        }
        if decider is None:
            async with ctx.session_factory() as session:
                await self._record(session, bot, chat_id, event_id, mode, "unconfigured")
                await self._finish(session, ctx, "succeeded", None, {"skipped": "unconfigured"})
            return
        decision = await decider.should_interject(payload)
        if decision.error:
            outcome = "error"
        elif decision.refused:
            outcome = "refused"
        elif decision.probability is None or decision.probability <= threshold:
            outcome = "below_threshold"
        else:
            outcome = "interject"
        anchor = next((line for line in reversed(lines) if line.event_id is not None), None)
        async with ctx.session_factory() as session:
            row = await self._record(
                session, bot, chat_id, event_id, mode, outcome, decision=decision, state=payload
            )
            turn: Task | None = None
            if outcome == "interject" and mode == "on" and anchor is not None:
                if await self._busy(session, bot.id, chat_id):
                    # 判断期间有人 @ 了它：交给那一轮。
                    row.outcome = "busy"
                else:
                    turn = await self._enqueue_turn(session, bot, chat_id, anchor.event_id, row.id)
                    row.task_id = turn.id if turn else None
            await self._finish(
                session, ctx, "succeeded", None, {"outcome": row.outcome, "decision_id": row.id}
            )
        ctx.log.info(
            "group_observe_decided",
            outcome=outcome,
            mode=mode,
            probability=decision.probability,
            latency_ms=decision.latency_ms,
        )

    async def _busy(self, session: Any, bot_id: Any, chat_id: str) -> bool:
        return bool(await tasks.open_for_session(session, bot_id, chat_id, "chat"))

    async def _enqueue_turn(
        self, session: Any, bot: Bot, chat_id: str, event_id: int | None, decision_id: int
    ) -> Task | None:
        event = await session.get(InboundEvent, event_id) if event_id else None
        if event is None:
            return None
        return await enqueue(
            session,
            NewTask(
                bot_id=bot.id,
                kind="chat",
                payload={
                    "message": event.payload,
                    "bot_key": bot.bot_key,
                    "platform_user_id": event.sender_platform_user_id,
                    "origin": "observe",
                    "observe_decision_id": decision_id,
                },
                session_key=chat_id,
                inbound_event_id=event.id,
                dedupe_key=f"observe-turn:{decision_id}",
            ),
        )

    async def _record(
        self,
        session: Any,
        bot: Bot,
        chat_id: str,
        event_id: int,
        mode: str,
        outcome: str,
        *,
        decision: decider_mod.Decision | None = None,
        state: dict[str, Any] | None = None,
    ) -> GroupObserveDecision:
        row = GroupObserveDecision(
            bot_id=bot.id,
            chat_id=chat_id,
            event_id=event_id or None,
            mode=mode,
            outcome=outcome,
            probability=decision.probability if decision else None,
            model=decision.model if decision else None,
            latency_ms=decision.latency_ms if decision else None,
            input_tokens=decision.input_tokens if decision else None,
            state=state or {},
        )
        session.add(row)
        await session.flush()
        return row

    async def _finish(
        self,
        session: Any,
        ctx: TaskContext,
        status: str,
        error_code: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        await tasks.finish(
            session,
            ctx.task.id,
            status=status,
            error_code=error_code,
            result=result,
            only_active=True,
        )
        await session.commit()
