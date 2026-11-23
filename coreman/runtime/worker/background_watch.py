"""后台阶段的观察任务（`background_watch`）与控制任务（`background_control`）。

观察任务接上节点的 `/live/attach`，一直跟到后台阶段结束：

- 后台任务表变化：更新 `background_runs.tasks` 与开工那一轮卡片上的区块（状态变化立刻刷新，
  只有进度变化时至少隔 30 秒）。
- 后台完成通知续出的回合（`turn_end` 且没有并入新消息）：作为新消息回复在原消息下并 @ 提问人，
  开头写明是哪一项完成了；用量累加，收尾时记一条对话记录（这里不调用模型，只转发）。
- 结束（`background_end`）：定终态、刷新卡片；被停、到期、进程异常时主动告诉提问人。

它不占 worker 的槽位、不参与会话串行（不带 session_key），新消息不会排在它后面。worker 排空
退出时就地交接：建下一个观察任务，自己按成功结束；驱动只保留最新的一个 attach 连接。

控制任务替网关、看护进程和管理接口调节点的 `/live/stop_task` 与 `/live/close`。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coreman.core.bus import outbox, tasks
from coreman.core.chat import background_runs, chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.db.models import BackgroundRun, Bot, RelayServer, RuntimeNode
from coreman.core.i18n.messages import msg
from coreman.core.relay.client import LiveNotFoundError, RelayError
from coreman.core.relay.sse import (
    BackgroundEndEvent,
    BackgroundEvent,
    BackgroundTasksEvent,
    LiveJoinedEvent,
    SseEvent,
    TextDelta,
    ToolUseStart,
    TurnEndEvent,
    UsageEvent,
    paragraph_gap,
)
from coreman.core.runtime_nodes.transport import online
from coreman.runtime.worker.context import TaskContext

# worker 排空时让观察任务交接的取消原因。
DRAIN_REASON = "worker_drain"
# 一条后台回复最多这么长（飞书富文本消息有体积上限），超出截断并提示看会话记录。
REPLY_CHARS = 20000
# attach 断开后重连的等待（秒），逐次翻倍到上限。
RECONNECT_SECONDS = (2.0, 5.0, 10.0, 20.0, 30.0)
# attach 返回 404、节点心跳却还列着这个会话时，最多再确认这么多次才判丢失。
NOT_LIVE_CHECKS = 3


@dataclass
class _Turn:
    """一个后台回合累计的正文与工具切分点。"""

    text: str = ""
    after_tool: bool = False
    finished: list[tuple[int, dict[str, Any]]] = field(default_factory=list)

    def add(self, delta: str) -> None:
        if self.after_tool:
            self.text += paragraph_gap(self.text, delta)
            self.after_tool = False
        self.text += delta


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    seen: bool = False

    def add(self, event: UsageEvent) -> None:
        self.seen = True
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cache_read_tokens += event.cache_read_tokens
        self.cache_creation_tokens += event.cache_creation_tokens


class _Ended(Exception):
    """后台阶段已经结束（流里收到 background_end，或平台已经定了终态）。"""


class BackgroundWatchHandler:
    kind = background_runs.WATCH_KIND

    def __init__(self, *, reconnect: tuple[float, ...] = RECONNECT_SECONDS) -> None:
        self.reconnect = reconnect

    async def run(self, ctx: TaskContext) -> None:
        run_id = int(ctx.task.payload.get("run_id") or 0)
        async with ctx.session_factory() as session:
            run = await session.get(BackgroundRun, run_id)
            relay = (
                await session.get(RelayServer, run.relay_server_id)
                if run is not None and run.relay_server_id
                else None
            )
            bot = await session.get(Bot, run.bot_id) if run is not None else None
        if run is None or bot is None or relay is None or run.state != "running":
            await self._finish(ctx, "succeeded", "not_running")
            return
        ctx.secrets = background_runs.open_secrets(ctx.cipher, run)
        watch = _Watch(ctx, run, bot, relay, self.reconnect)
        try:
            outcome = await watch.follow()
        except Exception as exc:
            ctx.log.exception("background_watch_crashed", run_id=run_id)
            await self._finish(ctx, "failed", type(exc).__name__)
            return
        if outcome == DRAIN_REASON:
            async with ctx.session_factory() as session:
                current = await session.get(BackgroundRun, run_id, populate_existing=True)
                if current is not None and current.state == "running":
                    generation = await background_runs.watch_generation(session, run_id)
                    await background_runs.enqueue_watch(session, current, generation=generation)
                await session.commit()
        await self._finish(ctx, "succeeded", outcome)

    async def _finish(self, ctx: TaskContext, status: str, outcome: str) -> None:
        async with ctx.session_factory() as session:
            await tasks.finish(
                session,
                ctx.task.id,
                status=status,
                result={"outcome": outcome},
                error_code=None if status == "succeeded" else outcome,
                only_active=True,
                bot_id=ctx.task.bot_id,
            )
            await session.commit()
        ctx.log.info("background_watch_done", status=status, outcome=outcome)


class _Watch:
    def __init__(
        self,
        ctx: TaskContext,
        run: BackgroundRun,
        bot: Bot,
        relay: RelayServer,
        reconnect: tuple[float, ...],
    ) -> None:
        self.ctx, self.run, self.bot, self.relay = ctx, run, bot, relay
        self.reconnect = reconnect
        self.usage = _Usage()
        self.delivered: list[str] = []
        self.card_seq = 0
        self.last_card = 0.0

    async def follow(self) -> str:
        """跟到结束；返回结局（终态名，或 `worker_drain` 表示交接给下一个观察任务）。"""
        attempt = 0
        not_live = 0
        while True:
            if self.ctx.cancel_event.is_set():
                return self.ctx.cancel_reason or "cancelled"
            if not await self._still_running():
                return "already_ended"
            try:
                await self._attach()
                attempt = 0
            except _Ended:
                return self.run.state
            except LiveNotFoundError:
                not_live += 1
                state = await self._not_live(not_live)
                if state is not None:
                    await self._end(state, self.run.tasks or [])
                    return state
            except RelayError as exc:
                self.ctx.log.warning("background_attach_failed", error=str(exc)[:200])
                if await self._node_gone():
                    await self._end("lost", self.run.tasks or [])
                    return "lost"
            delay = self.reconnect[min(attempt, len(self.reconnect) - 1)]
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.ctx.cancel_event.wait(), delay)

    async def _still_running(self) -> bool:
        async with self.ctx.session_factory() as session:
            current = await session.get(BackgroundRun, self.run.id, populate_existing=True)
        if current is None:
            return False
        self.run = current
        return current.state == "running"

    async def _attach(self) -> None:
        client = self.ctx.relay_client_factory(self.relay)
        remaining = (self.run.deadline - datetime.now(UTC)).total_seconds()
        # 到期之后还要等驱动收尾（close 后 30 秒内退出），多留几分钟。
        stream = client.live_attach(
            str(self.run.relay_session_id), total_timeout=max(remaining, 0) + 600
        )
        turn = _Turn()
        stopping = asyncio.ensure_future(self.ctx.cancel_event.wait())
        pending: asyncio.Task[SseEvent | None] | None = None
        try:
            while True:
                pending = asyncio.ensure_future(_next(stream))
                done, _ = await asyncio.wait(
                    {pending, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending not in done:
                    return
                event, pending = pending.result(), None
                if event is None:
                    return
                turn = await self._apply(event, turn)
        finally:
            stopping.cancel()
            if pending is not None:
                pending.cancel()
                await asyncio.wait({pending})
            await stream.aclose()
            await client.aclose()

    async def _apply(self, event: SseEvent, turn: _Turn) -> _Turn:
        if isinstance(event, TextDelta):
            turn.add(event.text)
        elif isinstance(event, ToolUseStart):
            turn.after_tool = True
        elif isinstance(event, UsageEvent):
            self.usage.add(event)
        elif isinstance(event, BackgroundTasksEvent | BackgroundEvent | LiveJoinedEvent):
            turn.finished += await self._snapshot(list(event.tasks))
        elif isinstance(event, TurnEndEvent):
            if not event.merged:
                await self._deliver(turn)
            return _Turn()
        elif isinstance(event, BackgroundEndEvent):
            if turn.text.strip():
                await self._deliver(turn)
            await self._end(self._end_state(event.state), list(event.tasks))
            raise _Ended
        return turn

    async def _snapshot(self, snapshot: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
        """合并快照；返回这次新结束的项。有状态变化立刻刷新卡片，否则至少隔 30 秒。"""
        snapshot = self.ctx.redact_json(snapshot)
        async with self.ctx.session_factory() as session:
            run = await session.get(
                BackgroundRun, self.run.id, with_for_update=True, populate_existing=True
            )
            if run is None or run.state != "running":
                await session.commit()
                raise _Ended
            old = list(run.tasks or [])
            finished = background_runs.finished_since(old, snapshot)
            merged = background_runs.merge(old, snapshot)
            changed = [(t.get("id"), t.get("status")) for t in old] != [
                (t.get("id"), t.get("status")) for t in merged
            ]
            run.tasks = merged
            run.updated_at = datetime.now(UTC)
            loop = asyncio.get_running_loop()
            refresh = changed or loop.time() - self.last_card >= (
                background_runs.CARD_INTERVAL_SECONDS
            )
            if refresh and merged != old:
                await self._queue_card(session, run)
                self.last_card = loop.time()
            await session.commit()
            self.run = run
        return finished

    async def _deliver(self, turn: _Turn) -> None:
        """一个后台回合：新消息回复在原消息下并 @ 提问人，开头写明哪一项完成了。"""
        body = (self.ctx.redact(turn.text) or "").strip()
        if not body and not turn.finished:
            return
        which = "、".join(background_runs.number(i) for i, _ in turn.finished)
        head = (
            msg("background_done_prefix", self.ctx.locale, which=which)
            if which
            else msg("background_turn_prefix", self.ctx.locale)
        )
        if len(body) > REPLY_CHARS:
            body = body[:REPLY_CHARS] + "\n\n…（内容较长已截断，完整内容见会话记录）"
        text = f"{head}\n\n{body}" if body else head
        async with self.ctx.session_factory() as session:
            run = await session.get(
                BackgroundRun, self.run.id, with_for_update=True, populate_existing=True
            )
            if run is None:
                await session.commit()
                return
            run.turns += 1
            await outbox.add(
                session,
                bot_id=run.bot_id,
                platform=run.platform,
                kind="send",
                dedupe_key=f"bg-turn:{run.id}:{run.turns}",
                target=background_runs.reply_target(run),
                payload={"markdown": text, "_mention_user_id": run.owner_platform_user_id},
            )
            await session.commit()
            self.run = run
        self.delivered.append(text)
        self.ctx.log.info("background_turn_delivered", run_id=self.run.id, turn=self.run.turns)

    def _end_state(self, driver_state: str) -> str:
        if driver_state == "finished":
            return "finished"
        if driver_state == "expired":
            return "expired"
        if driver_state == "stopped":
            return background_runs.CLOSE_STATES.get(self.run.close_reason or "", "stopped")
        # failed：进程异常退出，按节点中断告诉提问人。
        return "lost"

    async def _end(self, state: str, snapshot: list[dict[str, Any]]) -> None:
        """定终态、刷新卡片，被停、到期、丢失时主动告诉提问人；再记一条对话记录。"""
        snapshot = self.ctx.redact_json(snapshot)
        async with self.ctx.session_factory() as session:
            run = await session.get(
                BackgroundRun, self.run.id, with_for_update=True, populate_existing=True
            )
            if run is None:
                await session.commit()
                return
            run.tasks = background_runs.merge(run.tasks or [], snapshot)
            await background_runs.conclude(session, run, state, f"end:{self.ctx.task.id}")
            await self._log(session, run)
            await session.commit()
            self.run = run
        self.ctx.log.info("background_ended", run_id=self.run.id, state=self.run.state)

    async def _queue_card(self, session: Any, run: BackgroundRun) -> None:
        self.card_seq += 1
        await background_runs.queue_card(session, run, f"{self.ctx.task.id}:{self.card_seq}")

    async def _log(self, session: Any, run: BackgroundRun) -> None:
        """后台续出的回合记成一条对话记录：用量是这些回合的合计（并入新消息的那一轮已经自己记了）。"""
        if not self.delivered and not self.usage.seen:
            return
        now = datetime.now(UTC)
        entry = ChatLogEntry(
            bot_id=run.bot_id,
            bot_key=self.bot.bot_key,
            platform=run.platform,
            chat_type=run.chat_type,
            message_type="text",
            status="success",
            request_at=self.ctx.task.claimed_at or now,
            platform_user_id=run.owner_platform_user_id,
            user_id=run.owner_user_id,
            chat_id=run.chat_id,
            session_key=run.session_key,
            relay_session_id=run.relay_session_id,
            relay_server_id=run.relay_server_id,
            model=self.bot.model,
            task_id=self.ctx.task.id,
            message_content="（后台任务）",
            response_content="\n\n".join(self.delivered),
            latency_ms=int((now - (self.ctx.task.claimed_at or now)).total_seconds() * 1000),
            input_tokens=self.usage.input_tokens if self.usage.seen else None,
            output_tokens=self.usage.output_tokens if self.usage.seen else None,
            cache_read_tokens=self.usage.cache_read_tokens if self.usage.seen else None,
            cache_creation_tokens=self.usage.cache_creation_tokens if self.usage.seen else None,
            response_at=now,
        )
        await chat_logs.insert_turn(session, entry)
        self.delivered, self.usage = [], _Usage()

    async def _not_live(self, checks: int) -> str | None:
        """attach 返回 404：会话已不在后台阶段。返回要定的终态；None 表示再确认一次。"""
        if self.run.close_reason:
            return background_runs.CLOSE_STATES[self.run.close_reason]
        async with self.ctx.session_factory() as session:
            node = await session.get(RuntimeNode, self.run.node_id) if self.run.node_id else None
        listed = node is not None and str(self.run.relay_session_id) in (node.live_sessions or [])
        if listed and checks < NOT_LIVE_CHECKS:
            return None
        return "lost"

    async def _node_gone(self) -> bool:
        """节点离线超过宽限：判丢失。"""
        async with self.ctx.session_factory() as session:
            node = await session.get(RuntimeNode, self.run.node_id) if self.run.node_id else None
        if node is None or not node.is_active:
            return True
        if online(node) or node.heartbeat_at is None:
            return False
        gone = (datetime.now(UTC) - node.heartbeat_at).total_seconds()
        return gone >= background_runs.LOST_GRACE_SECONDS


async def _next(stream: Any) -> SseEvent | None:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


class BackgroundControlHandler:
    """调节点的 `/live/stop_task` 与 `/live/close`。会话已不在后台阶段（404）也算办完。"""

    kind = background_runs.CONTROL_KIND

    async def run(self, ctx: TaskContext) -> None:
        payload = ctx.task.payload
        async with ctx.session_factory() as session:
            run = await session.get(BackgroundRun, int(payload.get("run_id") or 0))
            relay = (
                await session.get(RelayServer, run.relay_server_id)
                if run is not None and run.relay_server_id
                else None
            )
        outcome = "skipped"
        status = "succeeded"
        if run is not None and relay is not None:
            client = ctx.relay_client_factory(relay)
            action = str(payload.get("action") or "")
            body: dict[str, Any] = {"session_id": str(run.relay_session_id)}
            if action == "stop_task":
                body["task_id"] = str(payload.get("task_id") or "")
            else:
                body["reason"] = str(payload.get("reason") or "user")
            try:
                outcome = "done" if await client.live_control(action, body) else "not_live"
            except RelayError as exc:
                ctx.log.warning("background_control_failed", action=action, error=str(exc)[:200])
                outcome, status = "relay_error", "failed"
            finally:
                await client.aclose()
            ctx.log.info("background_control", run_id=run.id, action=action, outcome=outcome)
        async with ctx.session_factory() as session:
            await tasks.finish(
                session,
                ctx.task.id,
                status=status,
                result={"outcome": outcome},
                error_code=None if status == "succeeded" else outcome,
                only_active=True,
                bot_id=ctx.task.bot_id,
            )
            await session.commit()


async def handle_card(
    session: Any, ctx: TaskContext, bot: Bot, inbound: Any, action: dict[str, Any]
) -> str:
    """后台任务区块上的「停止」（`level` 是后台任务 id）与「停掉全部后台任务」（`*`）。

    只认开工那一轮卡片上的点击（按投递记录里的消息 ID 核对），只有提问人与员工管理员能停。
    """
    from coreman.core.db.models import FeishuDelivery

    run_id = background_runs.parse_card_task_id(str(action.get("task_id") or ""))
    run = (
        await session.get(BackgroundRun, run_id, with_for_update=True, populate_existing=True)
        if run_id
        else None
    )
    card_task = (run.card_ref or {}).get("task_id") if run is not None else None
    delivery = await session.get(FeishuDelivery, card_task) if isinstance(card_task, int) else None
    if (
        run is None
        or run.bot_id != bot.id
        or delivery is None
        or not delivery.message_id
        or delivery.message_id != str(inbound.reply_context.get("message_id") or "")
    ):
        ctx.log.info("background_stop_ignored", run_id=run_id)
        return "ignored"
    clicker = str(ctx.task.payload.get("platform_user_id") or "")
    if not await background_runs.can_stop(session, run, clicker):
        return "denied"
    if run.state != "running":
        return "late"
    which = str(action.get("level") or "")
    if which == background_runs.ALL:
        done = await background_runs.request_close(session, run, "user")
    else:
        done = await background_runs.request_stop_task(session, run, which)
    return "stopping" if done else "late"
