"""平台投递时限、后台增量推送与独立的任务硬 TTL。

飞书不设置 agent_timeout：卡片网关关闭流式模式后继续更新原卡片。

`agent_timeout − pre_warning` 到达时先在思考区挂一行预警；再撑到 `agent_timeout` 就把流
置成 `delivery_mode=proactive` 并写下 `background_state`——网关据此把「当前正文 +
finish_suffix」作为 finish 推掉，此后不再跟这条流。worker 这边**不断开 SSE**，继续消费同
一轮对话，只是把增量改写进 outbox 主动推给用户，直到完成或硬 TTL 到期。

拆成两层是为了让规则可测：`BackgroundPusher` 是纯状态机（不碰库、不读真实时钟，时间一律
由调用方传入），`TimeoutSupervisor` 只负责把它的产物落进 `task_streams` / `outbox`。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from coreman.core.db.models import TaskStream
from coreman.core.i18n.messages import msg
from coreman.core.wecom.stream_render import truncate_utf8
from coreman.runtime.bus import outbox, streams
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.stream_writer import StreamWriter


@dataclass(frozen=True)
class Timing:
    """底层固定的投递与推送节奏；测试可整体缩短。

    Attributes:
        wecom_background_after: 企微流式窗口结束前的固定交接时刻
        pre_warning: 预警提前量，`agent_timeout` 减它就是预警时刻
        bg_min_interval: 后台增量两次推送的最小间隔
        bg_max_wait: 一直没有新工具边界时的兜底推送间隔
        bg_degrade_after: 推够这么多条就降级为低频摘要
        bg_degraded_interval: 降级后的推送间隔
        hard_ttl: 硬超时，到点取消任务
        max_bytes: 单条推送的字节上限（企微 stream 上限同值）
        silence_marks: 静默心跳的阈值（秒，升序）；空元组 = 不发心跳
    """

    # 企微长连接：单条流从首次发送起有效 600 秒，提前 20 秒交接。
    # https://developer.work.weixin.qq.com/document/path/101463
    wecom_background_after: float = 580.0
    pre_warning: float = 20.0
    bg_min_interval: float = 10.0
    bg_max_wait: float = 120.0
    bg_degrade_after: int = 24
    bg_degraded_interval: float = 90.0
    hard_ttl: float = 7200.0
    max_bytes: int = 20480
    # 提交轮（choice_submit）用：那一轮从创建就是主动推送，没有企微 stream 撑着「正在输入」，
    # 一句话都不吐的几分钟里用户只能干等，所以按静默时长补几声「还在跑」。
    silence_marks: tuple[float, ...] = ()


# B008：默认参数里不能直接 `Timing()`，用模块级单例（frozen 且无可变字段，共享安全）。
DEFAULT_TIMING = Timing()


class BackgroundPusher:
    """决定「这一刻该不该推、推哪一段」的纯状态机。

    `offset` 是已推送到的正文位置（切换后台的那一刻起算，之前的内容网关已经发过了）；
    `decide` 每次至多产出一条消息——企微主动推送有频控，宁可攒着也不要连发。

    Attributes:
        offset: 已推送到的 `pending_text` 下标
        pushed_count: 已推送的进展条数（不含降级通知与完成消息）
        degraded: 是否已切到低频摘要模式
    """

    def __init__(
        self,
        *,
        task_id: int,
        bot_id: uuid.UUID,
        platform: str,
        chat_id: str,
        verbosity_level: int,
        session_url: str,
        timing: Timing = DEFAULT_TIMING,
        locale: str = "zh",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.task_id = task_id
        self.bot_id = bot_id
        self.platform = platform
        self.chat_id = chat_id
        self.verbosity_level = verbosity_level
        self.session_url = session_url
        self.timing = timing
        self.locale = locale
        self.clock = clock
        self.offset = 0
        self.pushed_count = 0
        self.degraded = False
        self._last_push: float | None = None
        self._seq = 0
        self._started_at: float | None = None
        # 静默心跳的账：上一次「用户那边有动静」的时刻，以及已经报过的阈值。
        self._last_activity: float | None = None
        self._fired: set[float] = set()

    def start(self, now: float, pending_len: int) -> None:
        """切后台：记下起点，此刻之前的正文归网关的 finish 管，不重复推。"""
        self.offset, self._started_at, self._last_push = pending_len, now, now
        self._last_activity, self._fired = now, set()

    def next_dedupe_key(self) -> str:
        """幂等键 `{task_id}:send:{seq}`（spec §6.4 的格式）。"""
        self._seq += 1
        return f"{self.task_id}:send:{self._seq}"

    def cap(self, text: str) -> str:
        """超过 `max_bytes` 就按 UTF-8 边界截断并附上「看完整内容」的链接。"""
        suffix = msg("bg_truncated", self.locale, url=self.session_url)
        if len(text.encode()) <= self.timing.max_bytes:
            return text
        return truncate_utf8(text, self.timing.max_bytes - len(suffix.encode())) + suffix

    def decide(self, now: float, pending_text: str, boundaries: Sequence[int]) -> list[str]:
        """本次该推的消息（0 或 1 条）。verbosity >2 只在完成时推，全程返回空。"""
        if self.verbosity_level > 2 or self._started_at is None:
            return []
        since = now - (self._last_push if self._last_push is not None else self._started_at)
        new_boundaries = [b for b in boundaries if b > self.offset]
        if self.pushed_count >= self.timing.bg_degrade_after:
            if not self.degraded:
                # 降级通知只发一次，且不算进 pushed_count（它不是一段进展）。
                self.degraded, self._last_push = True, now
                return [msg("bg_degraded", self.locale, n=self.pushed_count, url=self.session_url)]
            if since < self.timing.bg_degraded_interval or len(pending_text) <= self.offset:
                return []
            end = len(pending_text)
        elif new_boundaries and since >= self.timing.bg_min_interval:
            # 按工具边界切：一次推完整的一段，不把句子切两半。
            end = new_boundaries[-1]
        elif len(pending_text) > self.offset and since >= self.timing.bg_max_wait:
            end = len(pending_text)
        else:
            return []
        delta = pending_text[self.offset : end]
        if not delta.strip():
            # 只有空白就别打扰用户了，但位置要推进，否则下一轮还会撞上同一段。
            self.offset = end
            return []
        self.offset, self._last_push = end, now
        self.pushed_count += 1
        # 推过一段就等于「用户刚收到动静」：静默从这一刻重新计，阈值也全部重新可用。
        self._last_activity, self._fired = now, set()
        return [self.cap(msg("bg_progress_prefix", self.locale) + delta)]

    def heartbeat(self, now: float) -> str | None:
        """提交轮静默心跳：静默跨过每个阈值各推一次；任何推送都让静默计时从头再来。

        每次至多产出一条：连着跨过两个阈值（一轮循环卡了很久）时也只报最早那个，下一轮再
        报下一个——连发两条「还在处理中」比不发更吵。
        """
        if not self.timing.silence_marks or self._started_at is None:
            return None
        base = self._last_activity if self._last_activity is not None else self._started_at
        silent = now - base
        for mark in self.timing.silence_marks:
            if silent >= mark and mark not in self._fired:
                self._fired.add(mark)
                # 报的是任务总耗时（用户关心「等了多久」），不是这一段静默的长度。
                return msg("submit_heartbeat", self.locale, seconds=int(now - self._started_at))
        return None

    def finish(self, final_text: str) -> list[str]:
        """完成时补推尾巴；verbosity >2 一次性推全文。"""
        if self.verbosity_level > 2:
            return [self.cap(final_text)] if final_text else []
        delta = final_text[self.offset :] if len(final_text) > self.offset else ""
        # The completed transcript already has a footer; proactive delivery
        # adds its own completion heading, so keep only one success marker.
        done = msg("done_suffix", self.locale)
        if delta.endswith(done):
            delta = delta[: -len(done)]
        if delta.strip():
            return [self.cap(msg("bg_done_prefix", self.locale) + delta)]
        return (
            [msg("bg_done_plain", self.locale)]
            if self.pushed_count or final_text.endswith(done)
            else []
        )

    def finish_plain(self, final_text: str) -> list[str]:
        """等待用户回答：只补尚未送达的正文和问题，不加成功标记。"""
        delta = final_text if self.verbosity_level > 2 else final_text[self.offset :]
        return [self.cap(delta)] if delta.strip() else []

    def finish_failed(self, text: str) -> list[str]:
        """失败 / 取消收尾：恒定一条，且绝不冠 ✅。

        `text` 是 `_classify` 已经拼好的终稿——取消时是「正文 + 停止后缀」，出错时是那句错误
        文案，都不能被说成「任务已完成」；只看字数差量的 `finish` 在这两种收尾上会把失败说成
        完成（错误文案比 offset 短，差量为空），甚至一条都不发。这里不看 offset，把终稿整条
        推出去，补一条会话链接（终稿里已经带了就不重复），再按 `max_bytes` 截断。
        verbosity >2 同理，没有「推全文」的特例。
        """
        link = (
            msg("session_link_suffix", self.locale, url=self.session_url)
            if self.session_url and self.session_url not in text
            else ""
        )
        return [self.cap(text + link)]

    def expired_message(self) -> str:
        link = (
            msg("session_link_suffix", self.locale, url=self.session_url)
            if self.session_url
            else ""
        )
        return msg("bg_ttl_expired", self.locale, link=link)


class TimeoutSupervisor:
    """把两阶段超时挂到消费循环上：判时刻、切模式、落库。

    `tick` 是协程（切换要写 `task_streams`、预警要 flush 思考区），调用方每轮循环调一次即
    可，粒度 ≤1 秒；relay 沉默时也必须调，否则超时永远等不到「下一帧」。
    """

    def __init__(
        self,
        ctx: TaskContext,
        writer: StreamWriter,
        *,
        agent_timeout: float | None,
        timing: Timing = DEFAULT_TIMING,
        verbosity_level: int = 1,
        session_url: str = "",
        chat_id: str = "",
        platform: str = "wecom",
        started_at: float | None = None,
    ) -> None:
        self.ctx = ctx
        self.writer = writer
        self.agent_timeout = float(agent_timeout) if agent_timeout is not None else None
        self.timing = timing
        self.verbosity_level = verbosity_level
        self.session_url = session_url
        self.chat_id = chat_id
        self.platform = platform
        self.started_at = ctx.clock() if started_at is None else started_at
        self.switched = False
        # 预警时刻已经是负的（agent_timeout 比提前量还短）就没有预警阶段，直接切。
        self._pre_warned = (
            self.agent_timeout is None or self.agent_timeout - timing.pre_warning <= 0
        )
        self._expired = False
        self.pusher = BackgroundPusher(
            task_id=ctx.task.id,
            bot_id=ctx.task.bot_id,
            platform=platform,
            chat_id=chat_id,
            verbosity_level=verbosity_level,
            session_url=session_url,
            timing=timing,
            locale=ctx.locale,
            clock=ctx.clock,
        )

    async def tick(self, now: float) -> str | None:
        """返回本轮发生的状态变化：`pre_warning` / `switched` / `expired` / None。"""
        elapsed = now - self.started_at
        delivery = self.writer.delivery
        if not self.switched and delivery is not None and delivery.delivery_mode == "proactive":
            self.switched, self._pre_warned = True, True
            self.pusher.start(now, delivery.offset)
            self.ctx.log.info("gateway_proactive_adopted", offset=delivery.offset)
            return "switched"
        if (
            self.agent_timeout is not None
            and not self._pre_warned
            and elapsed >= self.agent_timeout - self.timing.pre_warning
        ):
            self._pre_warned = True
            self.writer.set_thinking_line(msg("timeout_pre_warning", self.ctx.locale))
            await self.writer.flush(force=True)
            return "pre_warning"
        if (
            self.agent_timeout is not None
            and not self.switched
            and elapsed >= self.agent_timeout
        ):
            await self._switch(now)
            return "switched"
        if not self._expired and elapsed >= self.timing.hard_ttl:
            self._expired = True
            self.ctx.log.warning("hard_ttl_expired", elapsed_s=int(elapsed))
            return "expired"
        return None

    def start_proactive(self, now: float) -> None:
        """流从创建就是主动模式（提交轮）：不写库，直接进入后台推送状态。

        与 `_switch` 的区别是这里没有「网关推过一段」的历史——流是本轮自己建的，建的时候
        就写着 `delivery_mode=proactive`、`offset=0`，所以起点是 0，也没有超时预警要挂。
        """
        self.switched = True
        self._pre_warned = True
        self.pusher.start(now, 0)

    def finish_suffix(self) -> str:
        """网关给这条流补 finish 时追加的尾巴（超时提示 + 会话链接）。"""
        key = "timeout_background_low" if self.verbosity_level <= 2 else "timeout_background_high"
        link = (
            msg("session_link_suffix", self.ctx.locale, url=self.session_url)
            if self.session_url
            else ""
        )
        return msg(key, self.ctx.locale) + link

    async def _switch(self, now: float) -> None:
        """置 proactive 并写 background_state；SSE 不断，继续在后台消费。

        网关可能早就把这条流翻成 proactive 了（按 bot 排空、租约接管）。那时候它已经按自己
        记下的 `offset` 投递过一段（排空的 finish 带走了当时的正文，接管则一个字都没送出去），
        这里再按「当前正文长度」覆盖 offset，中间那一大段就永远不会被推给用户。所以先读行：
        已经是 proactive 就沿用它的 offset，只补上超时提示与切换时刻。

        读与写必须在同一个事务里、并且读的时候就上行锁：不上锁的话，网关那一笔可以正好挤
        在读与写之间提交，读到的仍是 `stream`，写下去的仍是「当前长度」，白读一场。
        """
        self.switched = True
        # 先强制落一次增量：网关的 finish 渲染的是库里的 pending_text，而 offset 取的是内存
        # 里的长度；不对齐的话，节流窗口里那点正文就会两头都不发。
        await self.writer.flush(force=True)
        async with self.ctx.session_factory() as session:
            row = await session.get(
                TaskStream, self.ctx.task.id, populate_existing=True, with_for_update=True
            )
            already = row is not None and row.delivery_mode == "proactive"
            offset = (
                streams.offset_of(row.background_state)
                if already and row
                else len(self.writer.pending_text)
            )
            state: dict[str, Any] = {
                "mode": "incremental" if self.verbosity_level <= 2 else "final_only",
                "offset": offset,
                "finish_suffix": self.finish_suffix(),
                "switched_at": datetime.now(UTC).isoformat(),
            }
            await streams.update(
                session, self.ctx.task.id, delivery_mode="proactive", background_state=state
            )
            await session.commit()
        self.pusher.start(now, offset)
        self.ctx.log.info(
            "switched_to_background", mode=state["mode"], offset=offset, taken_over=already
        )

    async def on_progress(self, now: float, pending_text: str, boundaries: Sequence[int]) -> None:
        if not self.switched:
            return
        for text in self.pusher.decide(now, pending_text, boundaries):
            await self.push(text)
        # 心跳排在增量之后：刚推完一段就不必再说「还在处理中」（decide 已经重置了静默计时）。
        beat = self.pusher.heartbeat(now)
        if beat is not None:
            await self.push(beat)

    async def on_finish(self, final_text: str, *, success: bool, waiting: bool = False) -> None:
        """收尾推送：成功走 `finish`（✅ + 尾巴增量），失败 / 取消走 `finish_failed`。"""
        if not self.switched:
            return
        produce = (
            self.pusher.finish_plain
            if waiting
            else (self.pusher.finish if success else self.pusher.finish_failed)
        )
        for text in produce(final_text):
            await self.push(text, dedupe_key=f"{self.ctx.task.id}:send:final")

    async def on_expired(self) -> None:
        await self.push(self.pusher.expired_message())

    async def push(self, markdown: str, dedupe_key: str | None = None) -> None:
        """一条推送一个短事务；dedupe_key 冲突说明已经排过队，静默忽略。"""
        async with self.ctx.session_factory() as session:
            await outbox.add(
                session,
                bot_id=self.ctx.task.bot_id,
                platform=self.platform,
                kind="send",
                dedupe_key=dedupe_key or self.pusher.next_dedupe_key(),
                target={"chat_id": self.chat_id},
                payload={"markdown": markdown},
            )
            await session.commit()
