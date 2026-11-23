"""租约限定的 CardKit 与出站投递，所有序号在调用平台前持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic as heading_clock
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from coreman.core import feishu_open
from coreman.core.assistants import sealed
from coreman.core.bus import outbox, streams
from coreman.core.chat import liveness
from coreman.core.chat.group_observe import NO_REPLY
from coreman.core.chat.reachability import private_target_valid
from coreman.core.db.models import (
    Bot,
    BotLease,
    FeishuDelivery,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
)
from coreman.core.feishu_cards.compile import (
    Compiled,
    compile_reply,
    plain_text,
    scan,
    simple_cards,
)
from coreman.core.feishu_cards.people import resolve_people
from coreman.core.feishu_cards.plan import PLAN_BOX_ID, plan_box
from coreman.core.feishu_cards.stream import StreamUnit, plan, stream_units
from coreman.core.feishu_cards.thinking import thinking_panel, thinking_preview
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.feishu_content import bounded, sent_text
from coreman.runtime.gateway_feishu import feed
from coreman.runtime.gateway_feishu.cards import (
    NEW_SESSION_LABEL_CHARS,
    STREAMING_CONFIG,
    interaction_card,
    media_card,
    new_session_card,
    post_content,
    remote_images_as_links,
    split_utf8,
    stop_button,
    stream_card,
    visible_parts,
)
from coreman.runtime.gateway_feishu.images import RemoteImages
from coreman.runtime.gateway_feishu.reactions import clean_finished, typing
from coreman.runtime.gateway_feishu.reply_buttons import expire_followups, mark_used

_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# 本 bot 的出站锁：同一时刻只有一个 child 能更新它的卡片、发它的出站条目。
_OUTPUT_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))")
# 常驻守护连接上用会话级锁：连接是 AUTOCOMMIT，不留未结束的事务，锁随连接关闭释放。
_OUTPUT_SESSION_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))")
_OUTPUT_SESSION_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:key,0))")
# 一轮最多发这么多条出站；没发完就告诉调用方接着来，别等兜底轮询。
OUTBOX_PER_ROUND = 20
# 同一 bot 两次平台调用之间至少隔这么久：每张卡片不超过飞书的 10 次/秒，整个应用也远低于
# 单接口 50 次/秒。推送按机器人串行（子进程只有两条库连接），所以间隔决定了并发回复时的刷新快慢。
CALL_SPACING_SECONDS = 0.125
# 流式模式开启 10 分钟后飞书自动关闭；提前续开，打字机效果不中断。
STREAM_RENEW_SECONDS = 9 * 60
# 流式卡片刚创建时的布局：只有一个占位的 answer 正文段。
INITIAL_LAYOUT = ({"kind": "text", "ids": ["answer"], "text": "…", "digest": ""},)
# 流式已被关闭：重新开启后重试。
STREAM_CLOSED = frozenset({200510, 200850, 300309})
# 卡片上的组件和记下的布局对不上（删不到、插不进）：整卡重建。
LAYOUT_DRIFT = frozenset({300121, 300301, 300314, 300315, 10002, 200220})
# 卡片内容本身有问题（字段不认、体积或元素超限）：原样重试没有意义，立即降级成简化卡。
# 一条回复最多上传这么多张远程图片，免得一条回复拖住整个出站循环。
IMAGES_PER_REPLY = 9
# 卡片内容本身有问题（字段不认、体积或元素超限、JSON 非法）：原样重试没有意义，立即降级。
# CardKit 与发消息接口各有一套码；-2 是本地判定的体积超限或 JSON 非法。
CARD_CONTENT_ERRORS = frozenset({-2, 10002, 200220, 200860, 300121, 300301, 300305, 230025, 230099})
# 机器人无法私信该用户（用户未与机器人建立可达会话等）；真机验证时核对错误码。
DM_UNREACHABLE = frozenset({230013})
# 建议追问的有效期：过了这么久点了不再发出，按钮置灰。
FOLLOWUPS_TTL_SECONDS = 30 * 60
# 收尾时没有正文可做预览，消息列表显示这一句（盖掉流式期间的进度预览）。
FINISHED_PREVIEW = "已回复"
log = get_logger(__name__)


def card_json(content: Any) -> str:
    """发给飞书的 JSON：紧凑写法（和编译器算体积的口径一致），NaN / Infinity 直接拒绝。"""
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def api_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FeishuError(-2, "invalid platform identifier")
    return value


def stable_uuid(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class LeaseLost(Exception):
    pass


class FeishuTransport:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        client: FeishuClient,
        *,
        bot_id: uuid.UUID,
        instance_id: str,
        generation: int,
        guard: AsyncConnection | None = None,
        credentials_fingerprint: str | None = None,
        image_transport: httpx.AsyncBaseTransport | None = None,
        lease_fence: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.factory, self.client = factory, client
        self.bot_id, self.instance_id, self.generation = bot_id, instance_id, generation
        self._last_call = 0.0
        # 每张卡最后推上去的状态行，和下次最早可以重试的时刻（推失败时退避）。
        self._headings: dict[int, tuple[str, float]] = {}
        # 流式期间推上去的消息列表预览：变了才改。
        self._previews: dict[int, str] = {}
        # 常驻连接（子进程的应用独占锁连接，AUTOCOMMIT）：出站锁在它上面以会话级锁拿一次，
        # 持有到进程退出，每一轮就不必再占一条池连接跨越整轮。
        self._guard = guard
        self.credentials_fingerprint = credentials_fingerprint
        # 个人助理记录没有自己的租约：连接按应用认领，围栏由应用连接进程提供（AppOutbound）。
        self._lease_fence = lease_fence
        self._output_held = False
        # 这一轮开头已经查过租约围栏：轮内的平台调用不再逐次回表。
        self._round_fenced = False
        self.images = RemoteImages(self.upload_image, transport=image_transport)
        # 机器人的富卡片开关；子进程每轮从库里刷新。
        self.rich_cards = True
        # 消息流卡片标题里的名字；子进程每轮刷新。
        self.bot_name = "AI 员工"
        # 做完置顶过的私聊（提问人 user_id）：本人下一次来问时取消置顶。重启后丢了由定时取消兜底。
        self._pinned: set[str] = set()
        # 这个机器人是个人助理：日志里不写卡片内容的片段。每轮开头刷新。
        self._private = False
        # 调度员在单人群里收到消息就打的「处理中」表情（见 acks.py）。
        from coreman.runtime.gateway_feishu.acks import Acks

        self.acks = Acks(self)

    async def fence(self) -> None:
        if self._lease_fence is not None:
            await self._lease_fence()
            return
        async with self.factory() as session:
            row = await session.scalar(
                select(BotLease.bot_id)
                .join(Bot, Bot.id == BotLease.bot_id)
                .where(
                    BotLease.bot_id == self.bot_id,
                    BotLease.holder_instance == self.instance_id,
                    BotLease.generation == self.generation,
                    Bot.enabled.is_(True),
                    BotLease.heartbeat_at > func.now() - timedelta(seconds=30),
                )
            )
            if row is None:
                raise LeaseLost

    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(max(0, self._last_call + CALL_SPACING_SECONDS - time.monotonic()))
        if not self._round_fenced:
            await self.fence()
        self._last_call = time.monotonic()
        return await self.client.call(method, path, **kwargs)

    async def upload_image(self, name: str, data: bytes, mime: str) -> str:
        result = await self.call(
            "POST",
            "/open-apis/im/v1/images",
            data={"image_type": "message"},
            files={"image": (name, data, mime)},
        )
        return api_id((result.get("data") or {}).get("image_key"))

    async def send(
        self,
        chat_id: str,
        content: dict[str, Any],
        *,
        kind: str,
        key: str,
        reply_to: str | None = None,
        receive_id_type: str = "chat_id",
    ) -> str:
        try:
            payload = card_json(content)
        except ValueError as exc:
            raise FeishuError(-2, "invalid message json") from exc
        if len(payload.encode()) > 30_000:
            raise FeishuError(-2, "message too large")
        body = {"msg_type": kind, "content": payload, "uuid": stable_uuid(key)}
        if reply_to:
            result = await self.call(
                "POST", f"/open-apis/im/v1/messages/{api_id(reply_to)}/reply", json=body
            )
        else:
            result = await self.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                json={**body, "receive_id": api_id(chat_id)},
            )
        data = result.get("data") or {}
        mid = api_id(data.get("message_id"))
        chat = data.get("chat_id") or (chat_id if receive_id_type == "chat_id" else "")
        await self.remember(mid, chat, sent_text(kind, content))
        return mid

    async def remember(
        self,
        message_id: object,
        chat_id: object,
        text: str | None,
        *,
        work_code: object = None,
    ) -> None:
        """发出消息的正文存档，有人引用它时 worker 从这里读；存不上不影响已经发出的消息。"""
        if not (text and text.strip() and isinstance(message_id, str) and message_id):
            return
        if not isinstance(chat_id, str) or not chat_id:
            return
        values = {"chat_id": chat_id, "text": bounded(text)}
        if isinstance(work_code, str) and work_code:
            values["work_code"] = work_code
        try:
            async with self.factory() as session:
                # 个人助理发出的正文加密存档；chat_id、work_code 不是内容列，原样写入。
                stored = await sealed.seal_values(session, FeishuSentMessage, self.bot_id, values)
                await session.execute(
                    insert(FeishuSentMessage)
                    .values(bot_id=self.bot_id, message_id=message_id, **stored)
                    .on_conflict_do_update(
                        index_elements=[FeishuSentMessage.bot_id, FeishuSentMessage.message_id],
                        set_=stored,
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 存档只为日后引用，不能让已发出的消息被当成失败重发
            log.warning("feishu_sent_record_failed", error=type(exc).__name__)

    async def round(self) -> bool:
        """推一轮流卡片、发一批出站；返回 True 表示出站还没发完（调用方别等兜底，接着来）。

        新旧 child 不能同时更新同一卡片：本 bot 的出站锁要么由构造时给的常驻连接持有（拿到
        一次就一直持有），要么在这里开一个跨越整轮的锁事务。租约围栏每轮只在开头查一次。
        """
        await self.fence()
        key = {"key": f"feishu-output:{self.bot_id}"}
        if self._guard is not None:
            if not self._output_held:
                self._output_held = bool(await self._guard.scalar(_OUTPUT_SESSION_LOCK, key))
            return self._output_held and await self._deliver()
        # 单独的锁事务跨越多个状态提交。
        async with self.factory() as guard:
            if not await guard.scalar(_OUTPUT_LOCK, key):
                return False
            return await self._deliver()

    async def release(self) -> None:
        """放掉常驻连接上的出站锁：这个 bot 不再由本进程发送（应用连接进程里记录被停用）。"""
        if self._guard is not None and self._output_held:
            await self._guard.execute(
                _OUTPUT_SESSION_UNLOCK, {"key": f"feishu-output:{self.bot_id}"}
            )
        self._output_held = False

    async def _deliver(self) -> bool:
        self._round_fenced = True
        await self.acks.expire()
        try:
            async with self.factory() as session:
                self._private = await sealed.guard_logs(session, self.bot_id)
                rows = list(
                    await session.scalars(
                        select(TaskStream)
                        .where(
                            TaskStream.bot_id == self.bot_id,
                            TaskStream.delivery_mode == "stream",
                            TaskStream.finish_pushed_at.is_(None),
                        )
                        .order_by(TaskStream.task_id)
                        .limit(50)
                    )
                )
                # 个人助理的内容还原成明文；读不出来的（数据密钥已销毁）不推，直接收尾：
                # 这条回复再也读不出来，留着不收尾每一轮都会被当成还有活。
                await sealed.open_rows(session, rows)
                locked = [row.task_id for row in rows if sealed.is_locked(row)]
                for task_id in locked:
                    await streams.mark_finish_pushed(session, task_id)
                if locked:
                    await session.commit()
                    log.warning("feishu_stream_locked", count=len(locked), code=sealed.LOCKED_CODE)
                rows = [row for row in rows if not sealed.is_locked(row)]
            for task_id in locked:
                # 入站消息上的「处理中」表情跟着撤掉（没打过就什么也不做）。
                await typing(self, task_id, done=True)
            active_ids = {row.task_id for row in rows}
            self._headings = {
                key: value for key, value in self._headings.items() if key in active_ids
            }
            self._previews = {
                key: value for key, value in self._previews.items() if key in active_ids
            }
            for row in rows:
                try:
                    await self.push(row)
                except LeaseLost:
                    raise
                except Exception as exc:  # noqa: BLE001 一条回复出问题不能卡住后面的回复
                    code = exc.code if isinstance(exc, FeishuError) else None
                    if code is None:
                        log.warning(
                            "feishu_push_failed", task_id=row.task_id, error=type(exc).__name__
                        )
                    async with self.factory() as session:
                        delivery = await session.get(FeishuDelivery, row.task_id)
                        if delivery is None:
                            continue
                        if code == 200810:
                            # 有人正在点这张卡：稍后再试，不算失败。
                            delivery.retry_at = datetime.now(UTC) + timedelta(seconds=2)
                            await session.commit()
                            continue
                        if code == 300317:
                            # 本地序号落后于飞书（接管、回滚）：一次抬高，下一轮就能继续。
                            delivery.sequence += 1000
                        delivery.failures += 1
                        delivery.last_error = str(exc) if code is not None else type(exc).__name__
                        delivery.retry_at = datetime.now(UTC) + timedelta(
                            seconds=min(120, 2 ** min(delivery.failures, 7))
                        )
                        if delivery.failures >= 6 or code in {230013, -2}:
                            delivery.fallback = True
                        await session.commit()
            await clean_finished(self)
            for _ in range(OUTBOX_PER_ROUND):
                if not await self.consume_one():
                    return False
            return True
        finally:
            self._round_fenced = False

    async def sequence(self, task_id: int) -> int:
        async with self.factory() as session:
            delivery = await session.get(FeishuDelivery, task_id, with_for_update=True)
            assert delivery is not None
            delivery.sequence += 1
            await session.commit()
            return delivery.sequence

    async def update_heading(self, row: TaskStream, card_id: str) -> str:
        """卡片顶部的状态行（思考面板的标题）：变了才改，不定时刷新。

        时间只精确到分钟，内容来自 worker 写下的最后动作，所以只在有新事件、跨过静默阈值、
        标上疑似卡住时才会变；收尾时整卡替换，不在这里改。
        """
        title = self.status_heading(row)
        if row.is_complete:
            self._headings.pop(row.task_id, None)
            return title
        now = heading_clock()
        previous = self._headings.get(row.task_id)
        if previous and (previous[0] == title or now < previous[1]):
            return title
        # Only patch the header: preserve the reader's expanded state and body.
        # Cosmetic failures must never prevent the real answer from being sent.
        try:
            await self.call(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/elements/thinking_panel",
                json={
                    "sequence": await self.sequence(row.task_id),
                    "partial_element": json.dumps(
                        {"header": {"title": {"tag": "markdown", "content": title}}},
                        ensure_ascii=False,
                    ),
                },
            )
        except FeishuError:
            self._headings[row.task_id] = (previous[0] if previous else "", now + 12)
            return title
        self._headings[row.task_id] = (title, 0.0)
        return title

    async def long_task_feed(self, row: TaskStream, delivery: FeishuDelivery) -> None:
        """群里的对话回复跑满 1 分钟：提问人的「进行中」消息流卡片，之后预览跟着最后动作走。

        一个群一张卡片（`chat_biz`），同一群的下一轮复用；私聊不建（私聊那一行自己就显示进展，
        做完临时置顶）。
        """
        requester, chat = _requester(row), str(row.reply_context.get("chat_id") or "")
        if requester is None or not chat or row.reply_context.get("chat_type") != "group":
            return
        if row.running_since is None:
            return
        if (datetime.now(UTC) - row.running_since).total_seconds() < feed.LONG_TASK_SECONDS:
            return
        state = dict(delivery.layout or {})
        preview = liveness.preview(row.activity, datetime.now(UTC)) or "正在处理…"
        biz = feishu_open.chat_biz(chat)
        if not state.get("feed"):
            created = await feed.upsert(
                self,
                biz_id=biz,
                user_id=requester,
                title=f"{self.bot_name}：正在处理你的请求",
                preview=preview,
                status=("进行中", "primary"),
                link=feed.open_link(self, chat, clear=(row.task_id, requester)),
            )
            state["feed"] = {"ok": created, "biz": biz, "preview": preview}
        elif state["feed"].get("ok") and state["feed"].get("preview") != preview:
            await feed.update_preview(self, biz_id=biz, user_id=requester, preview=preview)
            state["feed"] = {**state["feed"], "preview": preview}
        else:
            return
        async with self.factory() as session:
            saved = await session.get(FeishuDelivery, row.task_id)
            assert saved is not None
            saved.layout = state
            await session.commit()
        delivery.layout = state

    async def finish_long_task(
        self, session: AsyncSession, row: TaskStream, delivery: FeishuDelivery
    ) -> None:
        """回复做完：删掉「进行中」卡片；私聊里跑满 1 分钟的，把会话在消息列表临时置顶。

        「已完成」卡片不在这里建：成功与否、算不算长任务由 worker 判定，它发来的完成提醒带着
        `_long_done`，见 `long_done_feed`。
        """
        requester = _requester(row)
        if requester is None:
            return
        marker = (delivery.layout or {}).get("feed") or {}
        if marker.get("ok"):
            # 升级前建的「进行中」卡片按任务编号，没有 biz 记录。
            biz = str(marker.get("biz") or feed.task_biz(row.task_id))
            await feed.delete(self, biz_id=biz, user_id=requester)
        started, ended = row.running_since, row.completed_at or datetime.now(UTC)
        if (
            row.reply_context.get("chat_type") != "single"
            or started is None
            or (ended - started).total_seconds() < feed.LONG_TASK_SECONDS
        ):
            return
        if await feed.pin(self, requester, True):
            self._pinned.add(requester)
            await outbox.add(
                session,
                bot_id=self.bot_id,
                platform="feishu",
                kind="send",
                # 不以「任务号:」开头：不进这个任务的投递分组，免得 6 小时后才轮到完成提醒。
                dedupe_key=f"unpin:{row.task_id}",
                target={"user_id": requester},
                payload={"_unpin": requester},
                not_before=datetime.now(UTC) + timedelta(seconds=feed.PIN_SECONDS),
            )

    async def update_preview(self, row: TaskStream, card_id: str) -> None:
        """流式期间把消息列表的预览换成最后动作（「⏳ 运行命令：…」），不打开会话也看得到进展。

        变了才改；没有动作时不动，保留飞书默认的「[生成中...]」。改不上只记一笔，不影响正文。
        """
        text = liveness.preview(row.activity, datetime.now(UTC))
        if not text or self._previews.get(row.task_id) == text:
            return
        try:
            await self.call(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/settings",
                json={
                    "sequence": await self.sequence(row.task_id),
                    "settings": card_json({"config": {"summary": {"content": text}}}),
                },
            )
        except FeishuError as exc:
            log.warning("feishu_preview_update_failed", code=exc.code, task_id=row.task_id)
        self._previews[row.task_id] = text

    @staticmethod
    def status_heading(row: TaskStream) -> str:
        return liveness.status_heading(
            running_since=row.running_since,
            activity=row.activity,
            complete=row.is_complete,
            now=datetime.now(UTC),
        )

    async def _ensure_delivery(self, task_id: int, *, typing_done: bool = False) -> None:
        async with self.factory() as session:
            delivery = await session.get(FeishuDelivery, task_id)
            if delivery is None:
                delivery = FeishuDelivery(task_id=task_id, bot_id=self.bot_id)
                session.add(delivery)
            if typing_done:
                # 这一轮的表情另有人管（网关收到消息时打的），不再打。
                delivery.reaction_done = True
            await session.commit()

    async def push(self, row: TaskStream) -> None:
        # `_silent`：个人助理子任务的一轮，结论由平台另行带回主会话，这条流不发到飞书。
        if row.reply_context.get("_collaboration_helper") or row.reply_context.get("_silent"):
            if row.is_complete:
                async with self.factory() as session:
                    await streams.mark_finish_pushed(session, row.task_id)
                    await session.commit()
            return
        if row.reply_context.get("_observe"):
            # 群聊旁听的插话回合：不出「思考中」，终稿出来再整张发；agent 决定不说话就不发。
            # 调度回合一开始就在原消息上打「处理中」表情，不说话时也要撤掉。
            dispatching = bool(row.reply_context.get("_dispatch"))
            chat_id = row.reply_context.get("chat_id")
            anchor = row.reply_context.get("message_id")
            # 单人群里网关收到消息时已经打过表情：这一轮不再另打，结束时撤掉那一批。
            acked = dispatching and self.acks.has(chat_id, anchor)
            if not row.is_complete:
                if dispatching and not acked:
                    await self._ensure_delivery(row.task_id)
                    await typing(self, row.task_id)
                return
            final = row.final_text if row.final_text is not None else row.pending_text
            if visible_parts("", final)[1].strip() in ("", NO_REPLY):
                if acked:
                    await self.acks.clear(chat_id, anchor)
                elif dispatching:
                    await self._ensure_delivery(row.task_id)
                    await typing(self, row.task_id, done=True)
                async with self.factory() as session:
                    await streams.mark_finish_pushed(session, row.task_id)
                    await session.commit()
                return
            if acked:
                await self.acks.clear(chat_id, anchor)
                await self._ensure_delivery(row.task_id, typing_done=True)
        await self._ensure_delivery(row.task_id)
        await typing(self, row.task_id)
        async with self.factory() as session:
            delivery = await session.get(FeishuDelivery, row.task_id)
            assert delivery is not None
        if delivery.fallback or datetime.now(UTC) - delivery.created_at >= timedelta(days=13):
            if row.is_complete:
                async with self.factory() as session:
                    answer = row.final_text if row.final_text is not None else row.pending_text
                    await self.enqueue_final(
                        session, row, plain_text(visible_parts("", answer)[1]), start=0
                    )
                    await self.enqueue_interrupt_notice(session, row)
                    await streams.mark_finish_pushed(session, row.task_id)
                    await session.commit()
                if not visible_parts("", answer)[1].strip() and not row.pending_card:
                    await typing(self, row.task_id, done=True)
            return
        if delivery.retry_at and delivery.retry_at > datetime.now(UTC):
            return
        if not delivery.card_id:
            result = await self.call(
                "POST",
                "/open-apis/cardkit/v1/cards",
                json={
                    "type": "card_json",
                    "data": json.dumps(
                        stream_card(
                            "",
                            "",
                            session_url=row.session_url,
                            heading=self.status_heading(row),
                            stop_task=_stop_task(row),
                        ),
                        ensure_ascii=False,
                    ),
                },
            )
            delivery.card_id = api_id((result.get("data") or {}).get("card_id"))
            self._headings[row.task_id] = (self.status_heading(row), 0.0)
            async with self.factory() as session:
                await session.merge(delivery)
                await session.commit()
        card_id = api_id(delivery.card_id)
        if not delivery.message_id:
            requester = _requester(row)
            if requester and requester in self._pinned:
                # 本人又来问了：上一次做完时的置顶取消。
                self._pinned.discard(requester)
                await feed.pin(self, requester, False)
            delivery.message_id = await self.send(
                str(row.reply_context.get("chat_id") or ""),
                {"type": "card", "data": {"card_id": card_id}},
                kind="interactive",
                key=f"stream:{row.task_id}:initial",
                reply_to=row.reply_context.get("message_id"),
            )
            async with self.factory() as session:
                await session.merge(delivery)
                await session.commit()
        heading = await self.update_heading(row, card_id)
        if not row.is_complete and not delivery.is_static:
            await self.update_preview(row, card_id)
            await self.long_task_feed(row, delivery)
        if row.version <= row.pushed_version and not row.is_complete:
            return
        answer = (
            row.final_text if row.is_complete and row.final_text is not None else row.pending_text
        )
        thinking, answer = visible_parts(row.thinking_md, answer)
        if not row.is_complete and not delivery.is_static:
            await self.stream_update(row, delivery, card_id, thinking, answer, heading)
            if visible_parts("", answer)[1].strip():
                await typing(self, row.task_id, done=True)
            async with self.factory() as session:
                saved = await session.get(FeishuDelivery, row.task_id)
                assert saved is not None
                saved.failures, saved.retry_at, saved.last_error = 0, None, None
                await streams.mark_pushed(session, row.task_id, row.version)
                await session.commit()
            return
        compiled: Compiled | None = None
        summary = ""
        if row.is_complete:
            # 回复按钮只放在提问人自己的对话回复里：worker 开流时写下了这一轮的提问人。
            requester = row.reply_context.get("requester_user_id")
            compiled = await self.final_cards(
                thinking,
                answer,
                session_url=row.session_url,
                heading=heading,
                requester=requester if isinstance(requester, str) and requester else None,
                plan=row.plan,
                mobile=_mobile_private(row),
            )
            card, summary = compiled.cards[0], compiled.summary
        else:
            # 升级前就已改成整卡替换的流式卡片：沿用旧路径直到结束。
            card = stream_card(
                thinking, answer, streaming=False, session_url=row.session_url, heading=heading
            )
        if row.is_complete and not delivery.is_static:
            config: dict[str, Any] = {"streaming_mode": False}
            # 流式期间可能写过进度预览（「⏳ …」）：没有可做预览的正文时也要盖掉。
            summary = summary or FINISHED_PREVIEW
            if summary:
                # 关流式不会改写自定义摘要，消息列表的预览要在这里一起写上。
                config["summary"] = {"content": summary}
            await self.call(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/settings",
                json={
                    "sequence": await self.sequence(row.task_id),
                    "settings": json.dumps({"config": config}, ensure_ascii=False),
                },
            )
            async with self.factory() as session:
                saved = await session.get(FeishuDelivery, row.task_id)
                assert saved is not None
                saved.is_static = True
                await session.commit()
            delivery.is_static = True
        if delivery.is_static:
            try:
                await self.put_card(row.task_id, card_id, card)
            except FeishuError as exc:
                if compiled is None or exc.code not in CARD_CONTENT_ERRORS:
                    raise
                # 富卡片飞书不认：整条换成只有 markdown 的简化卡（放不下就分几张），回答完整送达。
                log.warning("feishu_rich_card_rejected", code=exc.code, task_id=row.task_id)
                compiled = await self.plain_cards(
                    thinking, answer, row.session_url, heading, plan=row.plan
                )
                await self.put_card(row.task_id, card_id, compiled.cards[0])
        if row.is_complete:
            # 流式卡片发出时只是个卡片实体引用，正文到这里才定下来。
            await self.remember(
                delivery.message_id,
                row.reply_context.get("chat_id"),
                answer,
                work_code=row.reply_context.get("_work_code"),
            )
        if visible_parts("", answer)[1].strip() or (row.is_complete and not row.pending_card):
            await typing(self, row.task_id, done=True)
        async with self.factory() as session:
            saved = await session.get(FeishuDelivery, row.task_id)
            assert saved is not None
            saved.failures, saved.retry_at, saved.last_error = 0, None, None
            await streams.mark_pushed(session, row.task_id, row.version)
            if row.is_complete:
                await self.finish_long_task(session, row, delivery)
                await self.enqueue_cards(session, row, compiled)
                await self.enqueue_followups_expiry(session, row, compiled, delivery.message_id)
                await self.enqueue_interrupt_notice(session, row)
                await streams.mark_finish_pushed(session, row.task_id)
            await session.commit()

    async def stream_update(
        self,
        row: TaskStream,
        delivery: FeishuDelivery,
        card_id: str,
        thinking: str,
        answer: str,
        heading: str,
    ) -> None:
        """流式期间的一次推送：思考面板变了才推，正文按单元增量更新（feishu_cards/stream.py）。

        卡片内容飞书不收（体积、组件数、字段）时暂停增量，只更新思考面板，收尾整卡替换。
        """
        state = dict(delivery.layout or {})
        now = time.time()
        since = float(state.get("since") or delivery.created_at.timestamp())
        if now - since >= STREAM_RENEW_SECONDS:
            await self.renew_streaming(row.task_id, card_id)
            state["since"] = now
        preview = thinking_preview(thinking)
        if preview != state.get("thinking"):
            await self.put_text(row.task_id, card_id, "thinking", preview)
            state["thinking"] = preview
        await self.push_plan(row, card_id, state)
        anchor = PLAN_BOX_ID if state.get("plan") else "thinking_panel"
        units = await asyncio.to_thread(stream_units, answer)
        # 还没有可显示的正文（只有思考、标题栏或半行围栏）：保持占位，别把 answer 删掉。
        if units and not state.get("paused"):
            layout = state["units"] if "units" in state else list(INITIAL_LAYOUT)
            shell = stream_card(thinking, "", session_url=row.session_url, heading=heading)
            shell["body"]["elements"] = _prefix(
                thinking, row.session_url, heading, row.plan, stop_task=_stop_task(row)
            )
            steps = plan(
                layout,
                units,
                anchor=anchor,
                shown=len(card_json(shell).encode()),
                show=remote_images_as_links,
            )
            try:
                if steps.actions:
                    try:
                        await self.call(
                            "POST",
                            f"/open-apis/cardkit/v1/cards/{card_id}/batch_update",
                            json={
                                "sequence": await self.sequence(row.task_id),
                                "actions": card_json(steps.actions),
                            },
                        )
                    except FeishuError as exc:
                        if exc.code not in LAYOUT_DRIFT:
                            raise
                        log.warning(
                            "feishu_stream_layout_rebuilt", code=exc.code, task_id=row.task_id
                        )
                        steps = await self.rebuild_stream(row, card_id, thinking, units, heading)
                for element_id, content in steps.texts:
                    await self.put_text(row.task_id, card_id, element_id, content)
                state["units"] = steps.layout
            except FeishuError as exc:
                if exc.code not in CARD_CONTENT_ERRORS | LAYOUT_DRIFT:
                    raise
                log.warning("feishu_stream_paused", code=exc.code, task_id=row.task_id)
                state["paused"] = True
        async with self.factory() as session:
            saved = await session.get(FeishuDelivery, row.task_id)
            assert saved is not None
            saved.layout = state
            await session.commit()
        delivery.layout = state

    async def rebuild_stream(
        self,
        row: TaskStream,
        card_id: str,
        thinking: str,
        units: list[StreamUnit],
        heading: str,
    ) -> Any:
        """布局对不上时整卡重建：流式模式保持开启，之后照常增量更新。"""
        shell = stream_card(thinking, "", session_url=row.session_url, heading=heading)
        prefix = _prefix(thinking, row.session_url, heading, row.plan, stop_task=_stop_task(row))
        shell["body"]["elements"] = prefix
        steps = plan(
            [],
            units,
            anchor=prefix[-1]["element_id"],
            shown=len(card_json(shell).encode()),
            show=remote_images_as_links,
        )
        elements = [e for action in steps.actions for e in action["params"].get("elements") or []]
        shell["body"]["elements"] = [*prefix, *elements]
        shell["config"]["streaming_mode"] = True
        await self.put_card(row.task_id, card_id, shell)
        steps.actions, steps.texts = [], []
        return steps

    async def push_plan(self, row: TaskStream, card_id: str, state: dict[str, Any]) -> None:
        """任务清单变了就整块替换（第一次插在思考面板后面）；推不上不影响正文。"""
        box = plan_box(row.plan)
        if box is None or row.plan == state.get("plan"):
            return
        try:
            if state.get("plan"):
                await self.call(
                    "PUT",
                    f"/open-apis/cardkit/v1/cards/{card_id}/elements/{PLAN_BOX_ID}",
                    json={"sequence": await self.sequence(row.task_id), "element": card_json(box)},
                )
            else:
                await self.call(
                    "POST",
                    f"/open-apis/cardkit/v1/cards/{card_id}/elements",
                    json={
                        "type": "insert_after",
                        "target_element_id": "thinking_panel",
                        "sequence": await self.sequence(row.task_id),
                        "elements": card_json([box]),
                    },
                )
        except FeishuError as exc:
            log.warning("feishu_plan_push_failed", code=exc.code, task_id=row.task_id)
            return
        state["plan"] = list(row.plan)

    async def renew_streaming(self, task_id: int, card_id: str) -> None:
        """重新开启流式模式（飞书从这一刻重新计 10 分钟）。"""
        config = {"streaming_mode": True, "streaming_config": STREAMING_CONFIG}
        await self.call(
            "PATCH",
            f"/open-apis/cardkit/v1/cards/{card_id}/settings",
            json={
                "sequence": await self.sequence(task_id),
                "settings": card_json({"config": config}),
            },
        )

    async def put_text(self, task_id: int, card_id: str, element_id: str, content: str) -> None:
        """流式写入一段文本；流式模式已被关掉时续开后重试一次。"""
        path = f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content"
        try:
            await self.call(
                "PUT", path, json={"sequence": await self.sequence(task_id), "content": content}
            )
        except FeishuError as exc:
            if exc.code not in STREAM_CLOSED:
                raise
            await self.renew_streaming(task_id, card_id)
            await self.call(
                "PUT", path, json={"sequence": await self.sequence(task_id), "content": content}
            )

    async def put_card(self, task_id: int, card_id: str, card: dict[str, Any]) -> None:
        seq = await self.sequence(task_id)
        await self.call(
            "PUT",
            f"/open-apis/cardkit/v1/cards/{card_id}",
            json={
                "sequence": seq,
                "uuid": stable_uuid(f"card:{task_id}:{seq}"),
                "card": {"type": "card_json", "data": card_json(card)},
            },
        )

    async def final_cards(
        self,
        thinking: str,
        answer: str,
        *,
        session_url: str | None,
        heading: str,
        requester: str | None = None,
        plan: Sequence[dict[str, str]] = (),
        mobile: bool = False,
    ) -> Compiled:
        """终稿编译成若干张卡；关掉富卡片或编译自检不过时用简化卡。"""
        prefix = _prefix(thinking, session_url, heading, plan)
        # 正文图片照旧内嵌并另起一行保留原图链接；块里的图片（实体卡等）再单独换 key。
        answer = await self.images.localize(answer)
        if self.rich_cards:
            # 切块与编译都是纯 CPU 活，放到线程里，别让超长回复挡住租约心跳和其他回复。
            urls, names = await asyncio.to_thread(scan, answer)
            images = await self.image_keys(urls)
            people = await self.people_ids(names)
            compiled = await asyncio.to_thread(
                compile_reply,
                answer,
                prefix=prefix,
                images=images,
                people=people,
                requester=requester,
                reply_expires=int(time.time()) + FOLLOWUPS_TTL_SECONDS,
                mobile=mobile,
            )
            if not compiled.problems:
                return compiled
            self._warn_invalid_card(compiled.problems)
        return await self.plain_cards(
            thinking, answer, session_url, heading, localized=True, plan=plan
        )

    def _warn_invalid_card(self, problems: list[str]) -> None:
        if self._private:
            # 校验说明里带着卡片里的键与取值（模型写的内容）：个人助理的只记条数。
            log.warning("feishu_rich_card_invalid", problem_count=len(problems))
        else:
            log.warning("feishu_rich_card_invalid", problems=problems[:5])

    async def plain_cards(
        self,
        thinking: str,
        answer: str,
        session_url: str | None,
        heading: str,
        *,
        localized: bool = False,
        plan: Sequence[dict[str, str]] = (),
    ) -> Compiled:
        prefix = _prefix(thinking, session_url, heading, plan)
        body = answer if localized else await self.images.localize(answer)
        cards = await asyncio.to_thread(simple_cards, body, prefix=prefix)
        summary = str(((cards[0].get("config") or {}).get("summary") or {}).get("content") or "")
        texts = [_card_markdown(card) for card in cards]
        return Compiled(cards, summary, texts)

    async def image_keys(self, urls: list[str]) -> dict[str, str]:
        """回复里的远程图片逐个上传换 img_key；失败的不进映射，渲染时退化成链接。"""
        keys: dict[str, str] = {}
        for url in urls[:IMAGES_PER_REPLY]:
            key = await self.images.key_for(url)
            if key:
                keys[url] = key
        return keys

    async def people_ids(self, names: list[str]) -> dict[str, str]:
        """people 块与表格 person 列里的人换成飞书 user_id；查不到的按文字显示。"""
        if not names:
            return {}
        async with self.factory() as session:
            return await resolve_people(session, names)

    async def enqueue_cards(
        self, session: AsyncSession, row: TaskStream, compiled: Compiled | None
    ) -> None:
        """续卡（主卡装不下的部分）和待发的选择卡，依次排进出站队列。

        每张续卡带上自己的降级文本：飞书拒收时只把这一张改发成普通消息。
        """
        cards = compiled.cards[1:] if compiled else []
        texts = compiled.texts[1:] if compiled else []
        for index, card in enumerate(cards, 1):
            text = texts[index - 1] if index - 1 < len(texts) else ""
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"{row.task_id}:more:{index}",
                target={"chat_id": row.reply_context.get("chat_id")},
                payload={"card": card, "_fallback_markdown": text, "_typing_task_id": row.task_id},
            )
        await self.enqueue_final(session, row, "", start=0)

    async def enqueue_followups_expiry(
        self,
        session: AsyncSession,
        row: TaskStream,
        compiled: Compiled | None,
        message_id: str | None,
    ) -> None:
        """建议追问到期时把按钮置灰：排一条到点才发的出站。

        按钮在主卡上就按主卡的消息 ID 改；在续卡上，续卡此刻还没发出，记下它的出站去重键，
        到点再查它的消息 ID。过期本身由网关应答时按按钮里的时刻判定，置灰只是让人看得出来。
        """
        if compiled is None or compiled.followups is None:
            return
        index, buttons = compiled.followups
        target: dict[str, Any] = {"chat_id": row.reply_context.get("chat_id")}
        if index == 0:
            if not message_id:
                return
            target["message_id"] = message_id
        else:
            target["outbox_dedupe"] = f"{row.task_id}:more:{index}"
        await outbox.add(
            session,
            bot_id=row.bot_id,
            platform="feishu",
            kind="card_update",
            dedupe_key=f"{row.task_id}:followups-expire",
            target=target,
            payload={"_followups_expire": list(buttons)},
            not_before=datetime.now(UTC) + timedelta(seconds=FOLLOWUPS_TTL_SECONDS),
        )

    async def enqueue_interrupt_notice(self, session: AsyncSession, row: TaskStream) -> None:
        """中断了（失联、读超时、硬超时、恢复补的终态）：群里 @ 提问人一次。

        卡片标题已经标红，单聊里看得到；群里卡片容易被刷上去，提问人不一定在看，所以 @ 一下。
        幂等键按任务定，收尸、恢复重复走到这里也只发一条。
        """
        reason = (row.activity or {}).get("interrupted")
        requester = row.reply_context.get("requester_user_id")
        if (
            not isinstance(reason, str)
            or not reason
            or row.reply_context.get("chat_type") != "group"
            or not isinstance(requester, str)
            or not requester
        ):
            return
        await outbox.add(
            session,
            bot_id=row.bot_id,
            platform="feishu",
            kind="send",
            dedupe_key=f"{row.task_id}:interrupted",
            target={
                "chat_id": row.reply_context.get("chat_id"),
                "message_id": row.reply_context.get("message_id"),
            },
            payload={
                "markdown": liveness.interrupt_notice(reason),
                "_mention_user_id": requester,
            },
        )

    async def enqueue_final(
        self, session: AsyncSession, row: TaskStream, answer: str, *, start: int
    ) -> None:
        for index, chunk in enumerate(split_utf8(answer)[start:] if answer.strip() else [], start):
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"{row.task_id}:overflow:{index}",
                target={"chat_id": row.reply_context.get("chat_id")},
                payload={"markdown": chunk, "_typing_task_id": row.task_id, "_plain": True},
            )
        if row.pending_card:
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"{row.task_id}:card:0",
                target={"chat_id": row.reply_context.get("chat_id")},
                payload={"card": row.pending_card, "_typing_task_id": row.task_id},
            )

    async def consume_one(self) -> bool:
        delivered: tuple[str, str, str] | None = None
        async with self.factory() as session:
            item = await outbox.claim_next(session, bot_id=self.bot_id)
            if item is None:
                return False
            if sealed.is_locked(item):
                # 个人助理的数据密钥已销毁：内容读不出来，不发。
                await outbox.mark_skipped(session, item.id, sealed.LOCKED_CODE)
                await session.commit()
                return True
            if not await private_target_valid(session, item):
                await outbox.mark_skipped(session, item.id, "recipient binding changed")
                await session.commit()
                return True
            try:
                if item.payload.get("_collaboration_setup_id"):
                    from coreman.core.chat.collaboration_setup import guard_probe

                    try:
                        if self.credentials_fingerprint is None:
                            raise ValueError("credentials_changed")
                        await guard_probe(
                            session, item, credentials_fingerprint=self.credentials_fingerprint
                        )
                    except ValueError as exc:
                        await outbox.mark_skipped(session, item.id, str(exc))
                        await session.commit()
                        return True
                if item.payload.get("_collaboration_id"):
                    from coreman.core.chat.bot_collaboration import ACTIVE, authorized
                    from coreman.core.db.models import BotCollaboration, BotCollaborationRoute

                    row = await session.get(
                        BotCollaboration,
                        uuid.UUID(item.payload["_collaboration_id"]),
                    )
                    try:
                        if row is None:
                            raise ValueError("collaboration missing")
                        route = await session.get(BotCollaborationRoute, row.route_id)
                        if route is None:
                            raise ValueError("collaboration route missing")
                        if row.status not in ACTIVE or row.expires_at <= datetime.now(UTC):
                            raise ValueError("collaboration inactive")
                        await authorized(
                            session, route, row.origin_platform_user_id, row.origin_user_id
                        )
                    except ValueError as exc:
                        await outbox.mark_skipped(session, item.id, str(exc))
                        await session.commit()
                        return True
                if item.payload.get("_human_collaboration_id"):
                    from coreman.core.db.models import HumanCollaboration

                    # Read only: this transaction holds the outbox row while sending, and close()
                    # may hold the ledger row while it waits for that outbox row.
                    asked = await session.get(
                        HumanCollaboration,
                        uuid.UUID(item.payload["_human_collaboration_id"]),
                        populate_existing=True,
                    )
                    # Questions and reminders stop once the ask ends; closing notices still go.
                    if asked is None or (
                        item.payload.get("_human_phase") in ("ask", "remind")
                        and asked.status != "waiting"
                    ):
                        await outbox.mark_skipped(session, item.id, "collaboration inactive")
                        await session.commit()
                        return True
                await self._send_item(item)
                if item.payload.get("_human_phase") in ("ask", "remind"):
                    delivered = (
                        str(item.payload["_human_collaboration_id"]),
                        str(item.payload["_human_phase"]),
                        str(item.payload["_feishu_message_id"]),
                    )
                if item.payload.get("_typing_task_id"):
                    await typing(self, int(item.payload["_typing_task_id"]), done=True)
                await session.flush()
                await outbox.mark_sent(session, item.id)
            except FeishuError as exc:
                if exc.code in {230013, -2}:
                    await outbox.fail(session, item.id, str(exc))
                    status = "failed"
                else:
                    status = await outbox.mark_failed(session, item.id, str(exc))
                if status == "failed" and item.payload.get("_typing_task_id"):
                    await typing(self, int(item.payload["_typing_task_id"]), done=True)
            except LeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 一条出站出问题不能卡住后面的出站
                log.warning("feishu_outbox_failed", item_id=item.id, error=type(exc).__name__)
                status = await outbox.mark_failed(session, item.id, type(exc).__name__)
                if status == "failed" and item.payload.get("_typing_task_id"):
                    await typing(self, int(item.payload["_typing_task_id"]), done=True)
            await session.commit()
        if delivered is not None:
            from coreman.core.chat.human_collaboration import record_delivery

            # Only after the outbox commit: the ledger lock is never taken while holding the
            # outbox row. A crash in between is backfilled from the outbox by the scheduler.
            async with self.factory() as session:
                await record_delivery(session, uuid.UUID(delivered[0]), *delivered[1:])
                await session.commit()
        return True

    async def _send_item(self, item: OutboxItem) -> None:
        if item.payload.get("_human_collaboration_id") or item.payload.get("_mention_user_id"):
            # The platform-owned at node is the only way a colleague is notified in a group.
            rows: list[list[dict[str, Any]]] = []
            if item.payload.get("_mention_user_id"):
                rows.append([{"tag": "at", "user_id": item.payload["_mention_user_id"]}])
            rows.append([{"tag": "md", "text": item.payload["markdown"]}])
            direct = item.target.get("user_id")
            mid = await self.send(
                str(direct or item.target.get("chat_id") or ""),
                {"zh_cn": {"title": "", "content": rows}},
                kind="post",
                key=f"outbox:{item.id}",
                reply_to=item.target.get("message_id"),
                receive_id_type="user_id" if direct else "chat_id",
            )
            item.payload = {**item.payload, "_feishu_message_id": mid}
            return
        if item.payload.get("_collaboration_id") or item.payload.get("_collaboration_setup_id"):
            # A separate at node preserves a real notification; Markdown occupies its own row.
            # Keep old durable text items deliverable during a rolling upgrade.
            rich = "markdown" in item.payload
            content = (
                {
                    "zh_cn": {
                        "title": "",
                        "content": [
                            [{"tag": "at", "user_id": item.payload["_mention_open_id"]}],
                            [{"tag": "md", "text": item.payload["markdown"]}],
                        ],
                    }
                }
                if rich
                else {"text": item.payload["text"]}
            )
            mid = await self.send(
                str(item.target.get("chat_id") or ""),
                content,
                kind="post" if rich else "text",
                key=f"outbox:{item.id}",
                reply_to=item.target.get("message_id"),
            )
            item.payload = {**item.payload, "_feishu_message_id": mid}
            return
        if isinstance(item.payload.get("_unpin"), str):
            self._pinned.discard(item.payload["_unpin"])
            await feed.pin(self, item.payload["_unpin"], False)
            return
        if isinstance(item.payload.get("_feed_delete"), dict):
            await self.expire_feed(item.payload["_feed_delete"])
            return
        if isinstance(item.payload.get("_long_done"), dict) and await self.long_done_feed(item):
            return
        if isinstance(item.payload.get("_file"), dict):
            item.payload = {
                **item.payload,
                "_feishu_message_id": await self.send_reply_file(item),
            }
            return
        if isinstance(item.payload.get("_new_session"), dict):
            item.payload = {
                **item.payload,
                "_feishu_message_id": await self.send_new_session(item),
            }
            return
        if item.kind == "card_update" and "_followups_expire" in item.payload:
            await expire_followups(self, item)
            return
        if item.kind == "card_update" and "_reply_used" in item.payload:
            await mark_used(self, item)
            return
        card = item.payload.get("card")
        if item.kind == "card_update":
            mid = api_id(item.target.get("message_id"))
            if not isinstance(card, dict):
                raise FeishuError(-2, "missing card")
            await self.call(
                "PATCH",
                f"/open-apis/im/v1/messages/{mid}",
                json={"content": json.dumps(interaction_card(card), ensure_ascii=False)},
            )
            await self.resolve_confirm_feed(mid)
            return
        if item.kind not in {"send", "welcome"}:
            raise FeishuError(-2, "unsupported outbox kind")
        chat = str(item.target.get("chat_id") or "")
        direct_user = item.target.get("user_id")
        if isinstance(card, dict) and direct_user:
            try:
                mid = await self.send(
                    str(direct_user),
                    interaction_card(card),
                    kind="interactive",
                    key=f"outbox:{item.id}",
                    receive_id_type="user_id",
                )
            except FeishuError as exc:
                back = item.payload.get("_dm_fallback")
                if exc.code not in DM_UNREACHABLE or not isinstance(back, dict):
                    raise
                # 私信不可达：回群里 @ 本人说明，卡片不再重发。
                await self.send(
                    str(back["chat_id"]),
                    {
                        "zh_cn": {
                            "title": "",
                            "content": [
                                [{"tag": "at", "user_id": back["user_id"]}],
                                [{"tag": "md", "text": back["text"]}],
                            ],
                        }
                    },
                    kind="post",
                    key=f"outbox:{item.id}:fallback",
                )
                return
            item.payload = {**item.payload, "_feishu_message_id": mid}
            await self.confirm_feed(item, card, mid)
            return
        if item.payload.get("_reply") and item.target.get("message_id"):
            # 群里对某条消息的简短回应（旁听插话的「我看一下」、排队提示、要不要停下上一条的
            # 选择卡片）：引用那条消息回复。
            mid = await self.send(
                chat,
                interaction_card(card)
                if isinstance(card, dict)
                else {"text": str(item.payload.get("text") or "")},
                kind="interactive" if isinstance(card, dict) else "text",
                key=f"outbox:{item.id}",
                reply_to=item.target.get("message_id"),
            )
            item.payload = {**item.payload, "_feishu_message_id": mid}
            if isinstance(card, dict):
                await self.confirm_feed(item, card, mid)
            return
        if isinstance(card, dict):
            fallback = item.payload.get("_fallback_markdown")
            try:
                mid = await self.send(
                    chat, interaction_card(card), kind="interactive", key=f"outbox:{item.id}"
                )
            except FeishuError as exc:
                if not isinstance(fallback, str) or exc.code not in CARD_CONTENT_ERRORS:
                    raise
                log.warning("feishu_outbox_card_rejected", code=exc.code, item_id=item.id)
                await self.send_posts(chat, fallback, f"outbox:{item.id}")
                return
            item.payload = {**item.payload, "_feishu_message_id": mid}
            await self.confirm_feed(item, card, mid)
        elif isinstance(item.payload.get("markdown"), str) and not item.payload.get("_plain"):
            # Markdown 消息（定时任务结果等）也优先用卡片；某张被拒只把那一张改发成 post。
            # 这些不是提问人自己的对话回复，回复按钮只显示成文字。
            await self.send_markdown_cards(chat, item.payload["markdown"], key=f"outbox:{item.id}")
        else:
            text = str(item.payload.get("markdown") or item.payload.get("text") or "…")
            await self.send_posts(chat, text, f"outbox:{item.id}")

    async def send_reply_file(self, item: OutboxItem) -> str:
        """回复里链接的本地文件：worker 已上传换好 file_key，这里引用提问发出。

        MP4、OPUS 读得出时长的放进一张单独的卡片直接播放（视频、音频组件要求卡片不能转发，所以不和
        回答放在同一张卡里）；客户端或飞书不认这张卡时改发文件消息。
        """
        meta = item.payload["_file"]
        chat = str(item.target.get("chat_id") or "")
        key = str(meta.get("file_key") or "")
        media = meta.get("media")
        if media in ("video", "audio"):
            try:
                return await self.send(
                    chat,
                    media_card(media, key, str(meta.get("name") or "")),
                    kind="interactive",
                    key=f"outbox:{item.id}",
                    reply_to=item.target.get("message_id"),
                )
            except FeishuError as exc:
                if exc.code not in CARD_CONTENT_ERRORS:
                    raise
                log.warning("feishu_media_card_rejected", code=exc.code, item_id=item.id)
        return await self.send(
            chat,
            {"file_key": key},
            kind="file",
            key=f"outbox:{item.id}:file",
            reply_to=item.target.get("message_id"),
        )

    async def send_new_session(self, item: OutboxItem) -> str:
        """重置会话的回执。私聊：系统消息「新会话」分割线并清屏（飞书只允许发到私聊，需要
        `im:message:send_sys_msg` 权限；发不出就改回文字回执）。群里：引用那条命令回一张灰色的
        分隔小卡片。"""
        meta = item.payload["_new_session"]
        chat = str(item.target.get("chat_id") or "")
        label = str(meta.get("label") or "")[:NEW_SESSION_LABEL_CHARS]
        if meta.get("single"):
            try:
                return await self.send(
                    chat,
                    {
                        "type": "divider",
                        "params": {"divider_text": {"text": label}},
                        "options": {"need_rollup": True},
                    },
                    kind="system",
                    key=f"outbox:{item.id}",
                )
            except FeishuError as exc:
                log.warning("feishu_new_session_divider_failed", code=exc.code, item_id=item.id)
                return await self.send(
                    chat,
                    {"text": str(item.payload.get("text") or label)},
                    kind="text",
                    key=f"outbox:{item.id}:text",
                )
        return await self.send(
            chat,
            new_session_card(label, str(meta.get("hint") or "")),
            kind="interactive",
            key=f"outbox:{item.id}",
            reply_to=item.target.get("message_id"),
        )

    async def confirm_feed(self, item: OutboxItem, card: dict[str, Any], mid: str) -> None:
        """等发起人确认的卡片发出去了：给发起人建一张「待确认」的消息流卡片（点过后删掉）。"""
        title = feed.confirm_card(card)
        if title is None:
            return
        user = item.target.get("user_id")
        if not isinstance(user, str) or not user:
            user = await self._initiator(card)
        if not user:
            return
        biz = feed.confirm_biz(mid)
        if await feed.create(
            self,
            biz_id=biz,
            user_id=user,
            title=f"{self.bot_name}：有一项操作等你确认",
            preview=title or "点开会话处理",
            status=("待确认", "danger"),
            link=feed.open_link(self, str(item.target.get("chat_id") or "") or None),
        ):
            item.payload = {**item.payload, "_feed": {"biz": biz, "user": user}}

    async def long_done_feed(self, item: OutboxItem) -> bool:
        """长任务完成提醒：不往会话里再发一条。返回 False 时照旧发 `markdown` 那条。

        - 群聊：提问人这个群的消息流卡片改成「已完成」并提醒一次，点开定位到这一轮的回复、
          顺带删掉卡片；没点开的 24 小时后删。
        - 私聊：收尾时已经把会话临时置顶，那就够了；置顶没成功才发文字。
        """
        meta = item.payload["_long_done"]
        user, task_id = meta.get("user"), meta.get("task_id")
        chat = str(item.target.get("chat_id") or "")
        if not isinstance(user, str) or not user or not isinstance(task_id, int) or not chat:
            return False
        if not meta.get("group"):
            return user in self._pinned
        seconds = f"耗时 {meta.get('seconds')} 秒"
        question = str(meta.get("question") or "")
        if question:
            preview = f"「{question}」· {seconds}"
        else:
            name = await self.chat_name(chat)
            preview = f"{seconds} · {name}" if name else seconds
        biz = feishu_open.chat_biz(chat)
        if not await feed.upsert(
            self,
            biz_id=biz,
            user_id=user,
            title=f"{self.bot_name}：回复已生成",
            preview=preview,
            status=("已完成", "success"),
            link=feed.open_link(
                self, chat, position=await self.reply_position(task_id), clear=(task_id, user)
            ),
            notify=True,
        ):
            return False
        async with self.factory() as session:
            await outbox.add(
                session,
                bot_id=self.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"feed_ttl:{task_id}",
                target={"user_id": user},
                payload={"_feed_delete": {"biz": biz, "user": user, "chat": chat, "task": task_id}},
                not_before=datetime.now(UTC) + timedelta(seconds=feed.DONE_SECONDS),
            )
            await session.commit()
        return True

    async def expire_feed(self, marker: dict[str, Any]) -> None:
        """删卡片。到期清理（带 `task`）时，这个群里同一提问人之后又有一轮就留着。

        一个群只有一张卡片：之后那一轮已经把它改写成自己的了，由那一轮负责删。
        """
        user, chat, task = marker.get("user"), marker.get("chat"), marker.get("task")
        if isinstance(chat, str) and isinstance(task, int):
            async with self.factory() as session:
                newer = await session.scalar(
                    select(TaskStream.task_id)
                    .where(
                        TaskStream.bot_id == self.bot_id,
                        TaskStream.task_id > task,
                        TaskStream.reply_context["chat_id"].astext == chat,
                        TaskStream.reply_context["requester_user_id"].astext == user,
                    )
                    .limit(1)
                )
            if newer is not None:
                return
        await feed.delete(self, biz_id=str(marker.get("biz")), user_id=str(user))

    async def reply_position(self, task_id: int) -> str | None:
        """这一轮回复卡片在会话里的位置（消息接口的 message_position），读不到返回 None。"""
        async with self.factory() as session:
            delivery = await session.get(FeishuDelivery, task_id)
        mid = delivery.message_id if delivery is not None else None
        if not mid:
            return None
        try:
            result = await self.call("GET", f"/open-apis/im/v1/messages/{api_id(mid)}")
        except FeishuError as exc:
            log.info("feishu_message_position_failed", code=exc.code)
            return None
        items = (result.get("data") or {}).get("items") or [{}]
        position = items[0].get("message_position")
        return str(position) if position not in (None, "") else None

    async def chat_name(self, chat_id: str) -> str:
        """群名；读不到（缺权限、机器人已出群）时返回空串。"""
        try:
            result = await self.call("GET", f"/open-apis/im/v1/chats/{api_id(chat_id)}")
        except FeishuError as exc:
            log.info("feishu_chat_name_failed", code=exc.code)
            return ""
        return str((result.get("data") or {}).get("name") or "")

    async def _initiator(self, card: dict[str, Any]) -> str | None:
        """确认卡的发起人：卡片 task_id 是 `personal:<发起那条消息的任务>`。"""
        kind, _, raw = str(card.get("task_id") or "").partition(":")
        if kind != "personal" or not raw.isdigit():
            return None
        async with self.factory() as session:
            origin = await session.get(Task, int(raw))
            if origin is None or origin.bot_id != self.bot_id or origin.inbound_event_id is None:
                return None
            event = await session.get(InboundEvent, origin.inbound_event_id)
            return event.sender_platform_user_id if event is not None else None

    async def resolve_confirm_feed(self, mid: str) -> None:
        """确认卡被更新（点过、作废）：删掉对应的「待确认」卡片。"""
        async with self.factory() as session:
            sent = await session.scalar(
                select(OutboxItem)
                .where(
                    OutboxItem.bot_id == self.bot_id,
                    OutboxItem.status == "sent",
                    OutboxItem.payload["_feishu_message_id"].astext == mid,
                    OutboxItem.payload.has_key("_feed"),
                )
                .order_by(OutboxItem.id.desc())
                .limit(1)
            )
            marker = sent.payload.get("_feed") if sent is not None else None
            if sent is None or not isinstance(marker, dict):
                return
            sent.payload = {k: v for k, v in sent.payload.items() if k != "_feed"}
            await session.commit()
        await feed.delete(self, biz_id=str(marker.get("biz")), user_id=str(marker.get("user")))

    async def send_posts(self, chat: str, markdown: str, key: str) -> None:
        for index, chunk in enumerate(split_utf8(markdown)):
            await self.send(
                chat,
                post_content(await self.images.localize(chunk)),
                kind="post",
                key=f"{key}:{index}",
            )

    async def send_markdown_cards(self, chat: str, markdown: str, *, key: str) -> None:
        """一段 Markdown 编译成若干张卡依次发出；富卡片自检不过时用简化卡。

        某张卡被飞书拒收时，只把这一张的降级文本改发成普通消息，已发出的不重发。
        """
        body = await self.images.localize(markdown)
        compiled: Compiled | None = None
        if self.rich_cards:
            urls, names = await asyncio.to_thread(scan, body)
            images = await self.image_keys(urls)
            people = await self.people_ids(names)
            compiled = await asyncio.to_thread(compile_reply, body, images=images, people=people)
            if compiled.problems:
                self._warn_invalid_card(compiled.problems)
                compiled = None
        if compiled is None:
            cards = await asyncio.to_thread(simple_cards, body)
            compiled = Compiled(cards, "", [_card_markdown(c) for c in cards])
        for index, card in enumerate(compiled.cards):
            try:
                await self.send(chat, card, kind="interactive", key=f"{key}:card:{index}")
            except FeishuError as exc:
                if exc.code not in CARD_CONTENT_ERRORS:
                    raise
                log.warning("feishu_card_rejected", code=exc.code, index=index)
                text = compiled.texts[index] if index < len(compiled.texts) else ""
                await self.send_posts(chat, text or plain_text(markdown), f"{key}:post:{index}")


def _card_markdown(card: dict[str, Any]) -> str:
    """简化卡里的正文（全是 markdown 组件）；改发普通消息时用。"""
    return "\n\n".join(
        str(e.get("content") or "")
        for e in (card.get("body") or {}).get("elements") or []
        if e.get("tag") == "markdown" and e.get("element_id") != "thinking"
    )


def _mobile_private(row: TaskStream) -> bool:
    """私聊、提问人在手机上：终稿按手机排。群里读的人设备不一，不按提问人的设备排。"""
    context = row.reply_context
    return context.get("chat_type") == "single" and context.get("client") == "mobile"


def _requester(row: TaskStream) -> str | None:
    requester = row.reply_context.get("requester_user_id")
    return requester if isinstance(requester, str) and requester else None


def _stop_task(row: TaskStream) -> int | None:
    """有提问人的对话回复才放「停止」按钮（定时任务、主动推送等没有人可停）。"""
    requester = row.reply_context.get("requester_user_id")
    return row.task_id if isinstance(requester, str) and requester else None


def _prefix(
    thinking: str,
    session_url: str | None,
    heading: str,
    plan: Sequence[dict[str, str]],
    *,
    stop_task: int | None = None,
) -> list[dict[str, Any]]:
    """卡片最前面的固定组件：思考面板（流式期间里面带停止按钮），有任务清单时再跟一个清单面板。"""
    box = plan_box(plan)
    stop = stop_button(stop_task) if stop_task is not None else None
    panel = thinking_panel(thinking, session_url=session_url, heading=heading, stop=stop)
    return [panel, box] if box else [panel]
