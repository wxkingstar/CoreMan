"""一个 bot 的运行体：长连接 + 入站入队 + 流推送 + 出站箱消费（spec §6.5、§7.3）。

`BotInfo` 是这个 bot 的运行期快照（含解密后的密钥，只在内存里）。租约认领成功时创建
`BotRunner`，释放租约时销毁；`bots` 行变化通过 `config_changed` 热更新：
名称/欢迎语原地改，凭证变化重连，停用或删除则整个销毁。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import leases, outbox, streams
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import Bot, TaskStream
from coreman.core.i18n.messages import msg
from coreman.core.logging import get_logger
from coreman.core.wecom.stream_render import StreamView, render_wecom_stream
from coreman.runtime.gateway_wecom.correlation import PushCorrelation
from coreman.runtime.gateway_wecom.inbound import enqueue_inbound, normalize_frame
from coreman.runtime.gateway_wecom.outbox_consumer import OutboxConsumer
from coreman.runtime.gateway_wecom.pusher import STREAM_DEAD, StreamPusher
from coreman.runtime.gateway_wecom.ws_client import WeComWsClient

if TYPE_CHECKING:  # 服务持有 runner，runner 只在类型层面引用服务，运行期不成环
    from coreman.runtime.gateway_wecom.service import GatewayWecomService

# 企微推送错误码（平台协议 §3.3）：频控；流已结束或不存在的那一组 STREAM_DEAD 与 pusher 同源。
RATE_LIMITED = 846607
# 「等一会儿再发就好」的那一族：频控 846607、接口调用超限 45009、系统繁忙 -1。
# 出站箱的条目撞上它们要放回队列重发；其余错误码（参数、凭证、会话不存在）重试也没用。
RETRYABLE = frozenset({RATE_LIMITED, 45009, -1})
RETRY_AFTER_SECONDS = 10.0
# 排空发过 finish 帧之后，关连接前留给企微回错误码的宽限：`ws.send` 成功不等于送到，
# 连接一断，846606/846608 就永远回不来，那一帧本该带走的正文也就永远没人补投。
ERRCODE_GRACE_SECONDS = 2.0


@dataclass
class BotInfo:
    """运行期需要的 bot 字段快照。

    `secret` 只在内存里流转：`repr=False` 是为了任何 `repr(info)`（异常、日志、断言）都带不出
    密钥；要比较凭证有没有变，用 `credentials_fingerprint`。
    """

    id: uuid.UUID
    bot_key: str
    name: str
    welcome_message: str | None
    wecom_bot_id: str
    secret: str = field(repr=False)
    credentials_fingerprint: str


def fingerprint(secret: str) -> str:
    """凭证指纹：只用于「变了没有」的比较，不可逆、不入库。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


async def load_bot_info(session: AsyncSession, cipher: Cipher, bot_id: uuid.UUID) -> BotInfo | None:
    """读一行 bot 并解密凭证；不存在 / 已停用 / 非企微 / 凭证不可用都返回 None。"""
    log = get_logger(__name__)
    bot = await session.get(Bot, bot_id, populate_existing=True)
    if bot is None or not bot.enabled or bot.platform != "wecom":
        return None
    try:
        creds = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    except (DecryptError, ValueError):
        log.error("bot_credentials_unreadable", bot_key=bot.bot_key)
        return None
    wecom_bot_id, secret = creds.get("bot_id", ""), creds.get("secret", "")
    if not wecom_bot_id or not secret:
        log.error("bot_credentials_incomplete", bot_key=bot.bot_key)
        return None
    return BotInfo(
        id=bot.id,
        bot_key=bot.bot_key,
        name=bot.name,
        welcome_message=bot.welcome_message,
        wecom_bot_id=wecom_bot_id,
        secret=secret,
        credentials_fingerprint=fingerprint(secret),
    )


class BotRunner:
    """一个 bot 的连接与收发。

    Attributes:
        bot: 运行期快照（热更新时整体替换）
        generation: 认领到的租约代次，出站箱与流推送都按它判断「是不是上一任留下的」
        lock: 串行化 `push_pending` / `consume` / `drain`——通知与兜底轮询会同时触发它们，
            而一条 WS 上的帧顺序是有意义的
    """

    def __init__(self, service: GatewayWecomService, bot: BotInfo, generation: int) -> None:
        self.service = service
        self.bot = bot
        self.generation = generation
        self.correlation = PushCorrelation()
        self.lock = asyncio.Lock()
        self.pusher = StreamPusher(self)
        self.outbox = OutboxConsumer(self)
        self.errcode_grace = ERRCODE_GRACE_SECONDS
        self._log = get_logger(__name__).bind(bot_key=bot.bot_key)
        self.ws = self._new_client()

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self.service.factory

    def _new_client(self) -> WeComWsClient:
        return WeComWsClient(
            bot_id=self.bot.wecom_bot_id,
            secret=self.bot.secret,
            bot_key=self.bot.bot_key,
            on_frame=self._on_frame,
            on_state=self._on_state,
            on_errcode=self._on_errcode,
            config=self.service.ws_config,
        )

    # ---- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        await self.ws.start()

    async def stop(self) -> None:
        await self.ws.stop()

    async def reconnect(self, bot: BotInfo | None = None) -> None:
        """凭证变了：旧连接必须断。熔断过的客户端是一次性的，所以整只换新（Task 10）。"""
        # 换连接期间不能有别的协程正拿着旧的 self.ws 发帧，所以整段在锁里做。
        async with self.lock:
            await self.ws.stop()
            if bot is not None:
                self.bot = bot
            self._log = get_logger(__name__).bind(bot_key=self.bot.bot_key)
            self.ws = self._new_client()
            await self.ws.start()

    async def drain(self) -> None:
        """单 bot 排空的 ①②（spec §6.5）：停出站消费，给未完成的流补一个 finish。

        推不出去（连接已经断了）也照样把流置成 proactive：这条流此后归 worker 的 outbox 管，
        不能留在「等网关推」的状态里。已经完成的流不翻 proactive——它只差最后一帧 finish，
        翻了反而变成「两边都以为对方会送终稿」。

        真发出去过 finish 帧的话，最后还要等一个有界的宽限：企微的错误码是异步回的，连接
        关早了，被拒的那一帧就没人知道，`pusher.note_stream_dead` 也就没机会补投缺口。
        宽限必须在事务提交之后等——不然错误码那一路读到的还是排空前的行，还会撞上行锁。
        """
        self.outbox.paused = True
        finished = False
        async with self.lock, self.factory() as session:
            now = datetime.now(UTC)
            for row in await streams.active_for_bot(session, self.bot.id):
                if row.finish_pushed_at is not None:
                    continue
                finished |= await self._drain_stream(session, row, now)
            await session.commit()
        if finished:
            await asyncio.sleep(self.errcode_grace)

    async def _drain_stream(self, session: AsyncSession, row: TaskStream, now: datetime) -> bool:
        """给一条流补 finish 并记下 offset；返回是否真的把 finish 帧发了出去。"""
        # 已完成的流没什么好「切换中」的（M-a）：补完那帧 finish 就收工。
        suffix = "" if row.is_complete else msg("drain_suffix")
        content = render_wecom_stream(StreamView.from_row(row), now, suffix=suffix)
        pushed = await self.pusher.send_stream(row, content, finish=True)
        if row.is_complete:
            if pushed:
                await streams.mark_pushed(session, row.task_id, row.version)
                await streams.mark_finish_pushed(session, row.task_id)
                # 终稿那一帧真发出去了：待发卡片得跟上。标完 finish 这一行就再也不会被
                # `pending_for_bot` 捞出来，这里不入队，卡片就永远烂在 `pending_card` 里。
                await self.pusher.queue_pending_card(session, row)
            else:
                # 连接已经断了，那帧 finish 没发出去：终稿改走 outbox。
                await self.pusher.deliver_final(
                    session, row.task_id, offset=self.pusher.delivered_len(row.stream_id)
                )
            self._log.info("stream_finished_for_drain", task_id=row.task_id, pushed=pushed)
            return pushed
        # 这帧 finish 把当前正文投递出去了，记成 offset，worker 的后台推送从这里接着来；
        # 没发出去的话只能认上一次确认送达的长度。
        offset = len(row.pending_text or "") if pushed else self.pusher.delivered_len(row.stream_id)
        state = {**(row.background_state or {}), "offset": offset, "finish_suffix": suffix}
        version = await streams.switch_to_proactive(session, row.task_id, state=state)
        if version is None:
            # worker 恰好在这中间收了尾：它看到的还是 stream，终稿得由网关送出去。
            await self.pusher.deliver_final(session, row.task_id, offset=offset)
            self._log.info("stream_completed_during_drain", task_id=row.task_id, offset=offset)
            return pushed
        await streams.mark_pushed(session, row.task_id, version)
        await streams.mark_finish_pushed(session, row.task_id)
        self._log.info("stream_finished_for_drain", task_id=row.task_id, offset=offset)
        return pushed

    # ---- 长连接回调 -------------------------------------------------------

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        """收帧任务里同步跑：只做归一化 + 入队，绝不在这里跑对话（Task 10 的背压点）。"""
        message = normalize_frame(
            self.bot, frame, gateway_instance=self.service.instance_id, now=datetime.now(UTC)
        )
        if message is None:
            return
        async with self.factory() as session:
            task = await enqueue_inbound(
                session, self.bot, message, lease_generation=self.generation
            )
            await session.commit()
        if task is not None:
            self._log.info(
                "inbound_enqueued", task_id=task.id, kind=task.kind, chat_type=message.chat_type
            )

    async def _on_state(self, state: str) -> None:
        async with self.factory() as session:
            await leases.set_state(
                session, self.bot.id, state, instance_id=self.service.instance_id
            )
            await session.commit()
        if state == "kicked" and self.ws.fused:
            # 熔断不可自愈：交回租约（服务在下一轮扫描里做，回调里不能 stop 自己所在的任务），
            # 冷却期内不再认领，免得和抢连的那一方打起来。M4 接告警。
            self._log.warning("wecom_kick_fused", kicks=len(self.ws.kick_times))
            self.service.note_fused(self.bot.id)

    async def _on_errcode(self, req_id: str, errcode: int, errmsg: str) -> None:
        """企微异步回的错误码：反查这次推送是什么，再按错误码退避 / 重排 / 转投。"""
        ctx = self.correlation.lookup(req_id) or {}
        extra = ctx.get("extra") or {}
        age = ctx.get("age")
        self._log.warning(
            "wecom_push_errcode",
            errcode=errcode,
            errmsg=errmsg[:200],
            action=ctx.get("action"),
            stream_id=ctx.get("stream_id"),
            age_s=round(float(age), 1) if age is not None else None,
        )
        chat_id = str(extra.get("chat_id") or "")
        stream_id = ctx.get("stream_id")
        outbox_id = extra.get("outbox_id")
        if outbox_id is not None:
            await self._reject_outbox(int(outbox_id), errcode, errmsg)
        if errcode == RATE_LIMITED and chat_id:
            self.outbox.note_rate_limited(chat_id)
        elif errcode in STREAM_DEAD and stream_id:
            task_id = extra.get("task_id")
            await self.pusher.note_stream_dead(
                str(stream_id), int(task_id) if task_id is not None else None
            )

    async def _reject_outbox(self, outbox_id: int, errcode: int, errmsg: str) -> None:
        """出站箱那条消息其实没发出去：`ws.send` 只是写进了连接，企微几秒后才回的错误码。

        标了 sent 就不管的话，频控（846607）挡下来的那条主动推送会静默消失——后台运行的长
        任务只有这一条通道，丢了用户就再也等不到答案。
        """
        async with self.factory() as session:
            status = await outbox.rejected(
                session,
                outbox_id,
                error=f"errcode={errcode} {errmsg[:200]}",
                retry_after=RETRY_AFTER_SECONDS if errcode in RETRYABLE else None,
            )
            await session.commit()
        self._log.warning(
            "outbox_rejected_by_platform", outbox_id=outbox_id, errcode=errcode, status=status
        )
