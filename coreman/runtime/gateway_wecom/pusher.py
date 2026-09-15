"""把 `task_streams` 的增量推成企微流式回复（spec §6.3、§7.3）。

worker 每写一次流就 `version+1` 并 `NOTIFY stream_updated`；网关按 `version > pushed_version`
取待推的行，渲染成 `aibot_respond_msg` 的 stream 帧发出去，再回写 `pushed_version`。

五条分支，顺序不能换（越靠前的越「终局」）：

1. **已 finish**：同一个 req_id 不可再推，认下版本就完事。
2. **已封流**：企微判这条流失效（846606/846608）。不能只是「不推」——终稿还在库里等人送：
   未完成的翻 proactive 交给 worker 的 outbox，已完成的由网关自己入 outbox 发出去。
3. **接管**：行是上一任持有者建的（`lease_generation` 更小），它的 `req_id` 属于那条已经断掉的
   连接，推不过去——同样是翻 proactive 或改走 outbox。
4. **已切后台**：worker 把 `delivery_mode` 置成了 proactive，渲染 `offset` 之前的正文 +
   `finish_suffix` 推一个 finish，此后不再跟这条流。
5. **已完成 / 进行中**：渲染终稿推 finish；进行中则节流 + 内容去重之后推 `finish=false`。

正文之后还可能有一张 `pending_card`（AskUserQuestion 的选项卡）。它只能跟在最后一段正文
**后面**：网关在终稿真的送出去之后（finish 帧推成功，或终稿改走 outbox）才入队，幂等键
`{task_id}:card:0` 与 worker 主动模式那一路相同，两边抢着入也只会发一张。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import WebSocketException

from coreman.core.bus import outbox, streams
from coreman.core.db.models import Task, TaskStream
from coreman.core.i18n.messages import msg
from coreman.core.logging import get_logger
from coreman.core.wecom.stream_render import StreamView, render_wecom_stream
from coreman.runtime.gateway_wecom.ws_client import (
    DeliveryNotSent,
    DeliveryRejected,
    DeliveryUncertain,
)

if TYPE_CHECKING:
    from coreman.runtime.gateway_wecom.runner import BotRunner


# 企微的错误码是异步回的：`ws.send` 成功只说明写进了连接，846606/846608 可能几秒后才回来。
# 这个预算之内发出去的帧一律不算「确认送达」——算多了，那一段就再也没人推了。
ERRCODE_DELAY_BUDGET = 3.0
# 每条流留这么多帧的「带走了多少正文」历史：够覆盖上面那个预算窗口，又不至于把内存吃住。
FRAME_HISTORY = 16
# 企微判这条流已经失效：流不存在或超时（846606）/ 流已结束（846608）。只有它们才封流。
STREAM_DEAD = frozenset({846606, 846608})
# 流收尾之后，帧历史与封流标记再留这么久：回执超时之后才到的错误码还要用它们算缺口；
# 过了这个窗口就清掉，常驻网关的内存不随处理过的流线性增长。
RETIRE_SECONDS = 60.0
# 一帧推送的结局（见 `_send_frame`）。
SENT, RETRY, DEAD, OFFLINE = "sent", "retry", "dead", "offline"


def _view_up_to(row: TaskStream, offset: int) -> StreamView:
    """切后台那一刻的视图：finish 帧只带走 `offset` 之前的正文。

    worker 切后台之后还在往 `pending_text` 里写，而 `offset` 之后的内容归它的主动推送。
    照库里的全量渲染的话，用户会先在流里看到一段、再在推送里看到同一段。
    """
    view = StreamView.from_row(row)
    view.pending_text = view.pending_text[:offset]
    view.final_text = None
    view.is_complete = False
    return view


class StreamPusher:
    """一个 bot 的流推送器。

    Attributes:
        min_interval: 同一条流两次「进行中」推送的最小间隔（完成/finish 不受它限制）
        errcode_budget: 错误码的异步延迟预算，这之内发出去的帧不算确认送达
        blocked: 被企微判定为已失效的 stream_id（846606/846608），不再往上推
    """

    def __init__(
        self,
        runner: BotRunner,
        *,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        errcode_budget: float = ERRCODE_DELAY_BUDGET,
    ) -> None:
        self.runner = runner
        self.min_interval = min_interval
        self.clock = clock
        self.errcode_budget = errcode_budget
        self.blocked: set[str] = set()
        self._last_push: dict[str, float] = {}
        self._last_content: dict[str, str] = {}
        # 每条流记一小段「这一帧什么时候带走了多少正文」的历史。企微的错误码回得很晚，而
        # 进行中的推送每秒就有一帧：只记最近两帧的话，被拒那一帧前面还压着好几帧，确认送达
        # 的长度会被估高——高出来的那一段 worker 不推、网关也不推，就此丢字。
        self._frames: dict[str, deque[tuple[float, int]]] = {}
        self._confirmed: dict[str, int] = {}
        # 已经走完的流 → 收尾时刻；`_sweep_retired` 按 RETIRE_SECONDS 清掉它们的全部内存状态。
        self._retired: dict[str, float] = {}
        self._log = get_logger(__name__).bind(bot_key=runner.bot.bot_key)

    def block(self, stream_id: str) -> None:
        """拉黑这条流，并就地冻结「能确认送达的长度」。

        错误码回得越晚，按延迟预算算出来的长度就越长；同一条流第二次收到错误码时再算一遍，
        会把第一次算出来的那段缺口悄悄吃掉。以第一次为准。
        """
        if stream_id not in self.blocked:
            self._confirmed[stream_id] = self._confirmed_now(stream_id)
        self.blocked.add(stream_id)

    def delivered_len(self, stream_id: str) -> int:
        """这条流上能确认送达用户的正文长度（没推过就是 0）。"""
        frozen = self._confirmed.get(stream_id)
        return self._confirmed_now(stream_id) if frozen is None else frozen

    def _confirmed_now(self, stream_id: str) -> int:
        """此刻能确认送达的长度：早于延迟预算的最后一帧带走了多少。

        宁可重复一小段，也不能算多——多算的那一段没有任何人会再推。
        """
        deadline = self.clock() - self.errcode_budget
        length = 0
        for sent_at, sent_len in self._frames.get(stream_id, ()):
            if sent_at > deadline:
                break
            length = sent_len
        return length

    def _retire(self, stream_id: str) -> None:
        """这条流在本网关上走完了：节流状态立刻丢，帧历史先留着给迟到的错误码用。"""
        self._last_push.pop(stream_id, None)
        self._last_content.pop(stream_id, None)
        self._retired.setdefault(stream_id, self.clock())

    def _sweep_retired(self) -> None:
        """收尾超过 RETIRE_SECONDS 的流：帧历史、确认长度、封流标记一并清掉。"""
        deadline = self.clock() - RETIRE_SECONDS
        for stream_id in [s for s, at in self._retired.items() if at <= deadline]:
            del self._retired[stream_id]
            self._frames.pop(stream_id, None)
            self._confirmed.pop(stream_id, None)
            self.blocked.discard(stream_id)

    async def note_stream_dead(self, stream_id: str, task_id: int | None) -> None:
        """企微回了 846606/846608：这条流没了，立刻把投递交出去，别等下一轮扫描。

        流死了之后 worker 可能一声不吭地跑上几十分钟（不再写增量就不会有 `stream_updated`），
        等扫描等于把这一轮的答案压在库里没人送。三种局面各有各的接盘人：

        1. 已完成 → 终稿就在库里，网关自己入 outbox（被拒的多半就是那一帧 finish）。
        2. 未完成、还没推过 finish → 翻 proactive，余下的正文与终稿归 worker 的主动推送。
        3. 未完成、finish 已经推过 → 行早就是 proactive 了，worker 此后只推 `[offset:]`，
           而那一帧 finish 其实没送到：`confirmed..offset` 这一段只能由网关补投。
        """
        self.block(stream_id)
        self._retire(stream_id)
        if task_id is None:
            return
        async with self.runner.factory() as session:
            row = await streams.get(session, task_id)
            if row is None:
                return
            confirmed = self.delivered_len(row.stream_id)
            if row.delivery_mode == "proactive":
                # 主动模式那一帧 finish 被拒：offset 之后归 worker，网关只补 confirmed..offset。
                await self.deliver_gap(session, row, confirmed=confirmed)
            elif row.is_complete:
                # 被拒的多半就是那一帧 finish：`finish_pushed_at` 已经标上了，可终稿其实
                # 一个字都没送到。改走 outbox（同一个幂等键，重复收到错误码也只发一条）。
                await self.deliver_final(session, task_id, offset=confirmed)
            else:
                await self.hand_off(session, row, reason="stream_dead")
            await session.commit()

    async def push_pending(self) -> None:
        """推完这一轮所有待推的流。锁防重入：通知与兜底轮询会同时叫它。"""
        async with self.runner.lock:
            self._sweep_retired()
            async with self.runner.factory() as session:
                now = datetime.now(UTC)
                for row in await streams.pending_for_bot(session, self.runner.bot.id):
                    if not await self._push_row(session, row, now):
                        # 连接断了，这一轮剩下的都别试了，等下一轮（不回写 pushed_version）。
                        break
                await session.commit()

    async def send_stream(self, row: TaskStream, content: str, *, finish: bool) -> bool:
        """发一帧 stream；返回这一帧是否确认送达（排空用：没送达就只认此前确认过的长度）。"""
        return await self._send_frame(row, content, finish=finish) == SENT

    async def _send_frame(self, row: TaskStream, content: str, *, finish: bool) -> str:
        """发一帧 stream 并等回执，返回结局：

        - `SENT`：平台确认收到；
        - `DEAD`：846606/846608，这条流已经失效——就地封流，调用方负责把投递交出去；
        - `RETRY`：其它错误码（系统繁忙、频控……），流还活着，下一轮用同一个 req_id 重推；
        - `OFFLINE`：没写出去或没等到回执（连接断了 / 回执超时），这一轮别再推这个 bot。
          不封流、不记帧：没确认的帧不计入已送达长度，重连后按版本号重推。
        """
        req_id = str((row.reply_context or {}).get("req_id") or "")
        if not req_id:
            self._log.warning("stream_without_req_id", task_id=row.task_id)
            return SENT
        frame: dict[str, Any] = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {"id": row.stream_id, "finish": finish, "content": content},
            },
        }
        try:
            await self.runner.ws.send_confirmed(frame)
        except DeliveryRejected as exc:
            if exc.errcode in STREAM_DEAD:
                self.block(row.stream_id)
                self._log.warning(
                    "stream_dead", task_id=row.task_id, errcode=exc.errcode, finish=finish
                )
                return DEAD
            self._log.warning(
                "stream_frame_rejected", task_id=row.task_id, errcode=exc.errcode, finish=finish
            )
            return RETRY
        except DeliveryUncertain as exc:
            self._log.warning("stream_not_confirmed", task_id=row.task_id, reason=str(exc))
            return OFFLINE
        except (DeliveryNotSent, RuntimeError, WebSocketException, OSError) as exc:
            self._log.info("stream_push_skipped", task_id=row.task_id, reason=type(exc).__name__)
            return OFFLINE
        self._frames.setdefault(row.stream_id, deque(maxlen=FRAME_HISTORY)).append(
            (self.clock(), len(row.pending_text or ""))
        )
        self.runner.correlation.put(
            req_id,
            bot_key=self.runner.bot.bot_key,
            action="stream",
            stream_id=row.stream_id,
            extra={"task_id": row.task_id, "finish": finish},
        )
        return SENT

    # ---- 单行分支 ---------------------------------------------------------

    async def _push_row(self, session: AsyncSession, row: TaskStream, now: datetime) -> bool:
        if row.finish_pushed_at is not None:
            # finish 之后同一个 req_id 不可再推（平台协议 §3.3 的硬限制）。
            await self._quiet(session, row, "stream_already_finished")
            return True
        if row.stream_id in self.blocked:
            await self.hand_off(session, row, reason="stream_blocked")
            return True
        if row.lease_generation < self.runner.generation:
            await self._takeover(session, row)
            return True
        if row.delivery_mode == "proactive":
            state = row.background_state or {}
            suffix = str(state.get("finish_suffix") or "")
            view = _view_up_to(row, streams.offset_of(state))
            return await self._finish(session, row, render_wecom_stream(view, now, suffix=suffix))
        if row.is_complete:
            return await self._finish(
                session, row, render_wecom_stream(StreamView.from_row(row), now)
            )
        return await self._progress(session, row, now)

    async def _on_dead(self, session: AsyncSession, row: TaskStream) -> None:
        """这一帧的回执就是 846606/846608：流当场失效，在同一轮里把投递交出去。

        不能留给下一轮的「已封流」分支去 `hand_off`：主动模式的行在那里会被当成「未完成待
        交接」，worker 记下的 offset 被改写，`confirmed..offset` 那一段就再也没人推了。
        """
        if row.delivery_mode == "stream" and row.is_complete:
            await self.deliver_final(session, row.task_id, offset=self.delivered_len(row.stream_id))
        else:
            await self.hand_off(session, row, reason="stream_dead")

    async def _takeover(self, session: AsyncSession, row: TaskStream) -> None:
        """上一任建的流：推不了，转交 worker 的 outbox（`_push_if_proactive` 会补终稿）。"""
        # offset=0：上一任的连接早就断了，这条流上一个字都没真的送到用户眼前。
        state = {
            **(row.background_state or {}),
            "offset": 0,
            "finish_suffix": msg("drain_suffix"),
            "takeover": True,
        }
        self._retire(row.stream_id)
        if await self._to_proactive(session, row, state) is None:
            # worker 抢在这一刻之前收了尾：它看到的还是 stream，终稿只能由网关送出去。
            await self.deliver_final(session, row.task_id, offset=0)
            return
        self._log.info(
            "stream_taken_over",
            task_id=row.task_id,
            row_generation=row.lease_generation,
            generation=self.runner.generation,
        )

    async def _to_proactive(
        self, session: AsyncSession, row: TaskStream, state: dict[str, Any]
    ) -> int | None:
        """翻 proactive 并就此收手（认下版本、标 finish）；行已完成时返回 None 什么都不改。"""
        version = await streams.switch_to_proactive(session, row.task_id, state=state)
        if version is None:
            return None
        await streams.mark_pushed(session, row.task_id, version)
        await streams.mark_finish_pushed(session, row.task_id)
        return version

    async def hand_off(self, session: AsyncSession, row: TaskStream, *, reason: str) -> None:
        """这条流再也推不出去了：把投递交出去，绝不是「标记一下就不管」。

        未完成 → 翻 proactive，剩下的内容与终稿归 worker 的后台推送；已完成 → 终稿就在库里，
        网关自己入 outbox 发出去（幂等键与 worker 的 `:send:final` 同一个，抢着发也只发一条）。
        """
        offset = self.delivered_len(row.stream_id)
        self._retire(row.stream_id)
        if row.delivery_mode == "proactive":
            # 行已经是主动模式：offset 是 worker 记下的「从哪里接着推」，改写它会丢掉
            # `confirmed..offset` 那一段。只补这段缺口，并认下 finish（这条流不再推了）。
            await self.deliver_gap(session, row, confirmed=offset)
            await streams.mark_pushed(session, row.task_id, row.version)
            if row.finish_pushed_at is None:
                await streams.mark_finish_pushed(session, row.task_id)
            self._log.info(reason, task_id=row.task_id, gap_from=offset)
            return
        if not row.is_complete:
            state = {
                **(row.background_state or {}),
                "mode": "proactive",
                "offset": offset,
                "finish_suffix": "",
                "switched_at": datetime.now(UTC).isoformat(),
            }
            if await self._to_proactive(session, row, state) is not None:
                self._log.info(reason, task_id=row.task_id, handed_off=True, offset=offset)
                return
        # 已完成（或刚刚在上一句之前完成）：终稿没人推了，网关自己发。
        await self.deliver_final(session, row.task_id, offset=offset)
        self._log.info(reason, task_id=row.task_id, sent_via="outbox", offset=offset)

    async def deliver_final(self, session: AsyncSession, task_id: int, *, offset: int) -> None:
        """终稿写好了、流却推不动：`final_text[offset:]` 入 outbox，标 finish 让 pusher 停手。"""
        row = await streams.get(session, task_id)
        if row is None:
            return
        final = row.final_text or ""
        task = await session.get(Task, task_id)
        successful = task is not None and task.status not in {"failed", "cancelled", "timed_out"}
        prefix = (row.pending_text or "")[:offset]
        text = (
            final[offset:]
            if successful and len(prefix) == offset and final.startswith(prefix)
            else final
        )
        chat_id = await self._chat_id(session, row)
        if not chat_id:
            # 没有会话 id 就真的没处送了：留一条 WARNING 给运维，别假装投递成功。
            self._log.warning("stream_final_undeliverable", task_id=task_id)
        elif text.strip():
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform=row.platform,
                kind="send",
                dedupe_key=f"{task_id}:send:final",
                target={"chat_id": chat_id},
                payload={"markdown": text},
            )
        await self.queue_pending_card(session, row)
        await streams.mark_pushed(session, task_id, row.version)
        if row.finish_pushed_at is None:
            await streams.mark_finish_pushed(session, task_id)
        self._retire(row.stream_id)

    async def queue_pending_card(self, session: AsyncSession, row: TaskStream) -> None:
        """流的最后一帧送达之后把待发卡片入出站箱；幂等键与 worker 主动模式那一路相同。"""
        card = row.pending_card
        if not isinstance(card, dict):
            return
        chat_id = await self._chat_id(session, row)
        if not chat_id:
            self._log.warning("pending_card_undeliverable", task_id=row.task_id)
            return
        await outbox.add(
            session,
            bot_id=row.bot_id,
            platform=row.platform,
            kind="send",
            dedupe_key=f"{row.task_id}:card:0",
            target={"chat_id": chat_id},
            payload={"card": card},
        )

    async def deliver_gap(self, session: AsyncSession, row: TaskStream, *, confirmed: int) -> None:
        """主动模式那一帧 finish 被拒了：把它本该带走的那一段（`confirmed..offset`）补投。

        行已经是 proactive、`finish_pushed_at` 也标上了，所以网关不会再推这条流；而 worker
        的主动推送从 `offset` 之后才开始——中间这一段没有任何人负责，不补就是丢答案。
        """
        offset = streams.offset_of(row.background_state)
        text = (row.pending_text or "")[confirmed:offset]
        if not text.strip():
            return
        chat_id = await self._chat_id(session, row)
        if not chat_id:
            # 没有会话 id 就真的没处送了：留一条 WARNING 给运维，别假装投递成功。
            self._log.warning("stream_gap_undeliverable", task_id=row.task_id)
            return
        await outbox.add(
            session,
            bot_id=row.bot_id,
            platform=row.platform,
            kind="send",
            dedupe_key=f"{row.task_id}:send:finish_gap",
            target={"chat_id": chat_id},
            payload={"markdown": text},
        )
        self._log.info(
            "stream_finish_gap_queued", task_id=row.task_id, confirmed=confirmed, offset=offset
        )

    async def _chat_id(self, session: AsyncSession, row: TaskStream) -> str:
        """主动推送要的会话 id：网关入站时写进 `reply_context`，老流回表用任务的会话键兜底。"""
        chat_id = str((row.reply_context or {}).get("chat_id") or "")
        if chat_id:
            return chat_id
        task = await session.get(Task, row.task_id)
        return str((task.session_key if task is not None else "") or "")

    async def _finish(self, session: AsyncSession, row: TaskStream, content: str) -> bool:
        result = await self._send_frame(row, content, finish=True)
        if result == DEAD:
            await self._on_dead(session, row)
            return True
        if result != SENT:
            return result == RETRY
        await streams.mark_pushed(session, row.task_id, row.version)
        await streams.mark_finish_pushed(session, row.task_id)
        if row.is_complete:
            # 主动模式下这一帧只是「切后台」的收尾，正文还没写完：卡片归 worker 自己入队
            # （同一个幂等键），网关此刻插一张只会比答案先到。
            await self.queue_pending_card(session, row)
        self._retire(row.stream_id)
        return True

    async def _progress(self, session: AsyncSession, row: TaskStream, now: datetime) -> bool:
        last = self._last_push.get(row.stream_id)
        tick = self.clock()
        if last is not None and tick - last < self.min_interval:
            return True  # 节流窗口内：不推也不回写，下一轮再说
        content = render_wecom_stream(StreamView.from_row(row), now)
        if content == self._last_content.get(row.stream_id):
            # 渲染结果没变（只改了不上屏的字段）：认下这个版本，别每轮都重算。
            await streams.mark_pushed(session, row.task_id, row.version)
            return True
        result = await self._send_frame(row, content, finish=False)
        if result == DEAD:
            await self._on_dead(session, row)
            return True
        if result == RETRY:
            self._last_push[row.stream_id] = tick  # 被拒也守节流，别每轮都砸同一个错误码
        if result != SENT:
            return result == RETRY
        self._last_push[row.stream_id] = tick
        self._last_content[row.stream_id] = content
        await streams.mark_pushed(session, row.task_id, row.version)
        return True

    async def _quiet(self, session: AsyncSession, row: TaskStream, reason: str) -> None:
        """这条流已经交接完了：认下版本，免得它每一轮都被捞出来。"""
        await streams.mark_pushed(session, row.task_id, row.version)
        self._retire(row.stream_id)
        self._log.debug(reason, task_id=row.task_id, stream_id=row.stream_id)
