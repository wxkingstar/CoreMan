"""租约限定的 CardKit 与出站投递，所有序号在调用平台前持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from time import monotonic as heading_clock
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from coreman.core.bus import outbox, streams
from coreman.core.chat.reachability import private_target_valid
from coreman.core.db.models import Bot, BotLease, FeishuDelivery, OutboxItem, TaskStream
from coreman.core.feishu_cards.compile import (
    Compiled,
    compile_reply,
    plain_text,
    scan,
    simple_cards,
)
from coreman.core.feishu_cards.people import resolve_people
from coreman.core.feishu_cards.stream import StreamUnit, plan, stream_units
from coreman.core.feishu_cards.thinking import thinking_panel, thinking_preview
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.cards import (
    STREAMING_CONFIG,
    interaction_card,
    post_content,
    remote_images_as_links,
    split_utf8,
    stream_card,
    visible_parts,
)
from coreman.runtime.gateway_feishu.images import RemoteImages
from coreman.runtime.gateway_feishu.reactions import clean_finished, typing
from coreman.runtime.gateway_feishu.reply_buttons import mark_used

_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# 本 bot 的出站锁：同一时刻只有一个 child 能更新它的卡片、发它的出站条目。
_OUTPUT_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))")
# 常驻守护连接上用会话级锁：连接是 AUTOCOMMIT，不留未结束的事务，锁随连接关闭释放。
_OUTPUT_SESSION_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))")
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
    ) -> None:
        self.factory, self.client = factory, client
        self.bot_id, self.instance_id, self.generation = bot_id, instance_id, generation
        self._last_call = 0.0
        self._headings: dict[int, tuple[float, int, str]] = {}
        # 常驻连接（子进程的应用独占锁连接，AUTOCOMMIT）：出站锁在它上面以会话级锁拿一次，
        # 持有到进程退出，每一轮就不必再占一条池连接跨越整轮。
        self._guard = guard
        self.credentials_fingerprint = credentials_fingerprint
        self._output_held = False
        # 这一轮开头已经查过租约围栏：轮内的平台调用不再逐次回表。
        self._round_fenced = False
        self.images = RemoteImages(self.upload_image, transport=image_transport)
        # 机器人的富卡片开关；子进程每轮从库里刷新。
        self.rich_cards = True

    async def fence(self) -> None:
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
        return api_id((result.get("data") or {}).get("message_id"))

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

    async def _deliver(self) -> bool:
        self._round_fenced = True
        try:
            async with self.factory() as session:
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
            active_ids = {row.task_id for row in rows}
            self._headings = {
                key: value for key, value in self._headings.items() if key in active_ids
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

    async def animate_heading(self, row: TaskStream, card_id: str) -> str:
        now = heading_clock()
        previous = self._headings.get(row.task_id)
        answer = (
            row.final_text if row.is_complete and row.final_text is not None else row.pending_text
        )
        active = not row.is_complete and not visible_parts("", answer)[1].strip()
        if previous and now - previous[0] < 3 and (active == bool(previous[2].endswith("."))):
            return previous[2]
        frame = (previous[1] + 1) % 3 if previous else 0
        title = "🤔 思考中" + "." * (frame + 1) if active else "🤔 思考过程"
        if previous and previous[2] == title:
            return title
        # Only patch the header: preserve the reader's expanded state and body.
        # Cosmetic failures must never prevent the real answer from being sent.
        self._headings[row.task_id] = (now, frame, title)
        try:
            await self.call(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/elements/thinking_panel",
                json={
                    "sequence": await self.sequence(row.task_id),
                    "partial_element": json.dumps(
                        {"header": {"title": {"tag": "plain_text", "content": title}}},
                        ensure_ascii=False,
                    ),
                },
            )
        except FeishuError:
            self._headings[row.task_id] = (now + 12, frame, title)
        if row.is_complete:
            self._headings.pop(row.task_id, None)
        return title

    async def push(self, row: TaskStream) -> None:
        if row.reply_context.get("_collaboration_helper"):
            if row.is_complete:
                async with self.factory() as session:
                    await streams.mark_finish_pushed(session, row.task_id)
                    await session.commit()
            return
        async with self.factory() as session:
            delivery = await session.get(FeishuDelivery, row.task_id)
            if delivery is None:
                delivery = FeishuDelivery(task_id=row.task_id)
                session.add(delivery)
                await session.commit()
                await session.refresh(delivery)
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
                            heading="🤔 思考过程" if row.is_complete else "🤔 思考中.",
                        ),
                        ensure_ascii=False,
                    ),
                },
            )
            delivery.card_id = api_id((result.get("data") or {}).get("card_id"))
            async with self.factory() as session:
                await session.merge(delivery)
                await session.commit()
        card_id = api_id(delivery.card_id)
        if not delivery.message_id:
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
        heading = await self.animate_heading(row, card_id)
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
            )
            card, summary = compiled.cards[0], compiled.summary
        else:
            # 升级前就已改成整卡替换的流式卡片：沿用旧路径直到结束。
            card = stream_card(
                thinking, answer, streaming=False, session_url=row.session_url, heading=heading
            )
        if row.is_complete and not delivery.is_static:
            config: dict[str, Any] = {"streaming_mode": False}
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
                compiled = await self.plain_cards(thinking, answer, row.session_url, heading)
                await self.put_card(row.task_id, card_id, compiled.cards[0])
        if visible_parts("", answer)[1].strip() or (row.is_complete and not row.pending_card):
            await typing(self, row.task_id, done=True)
        async with self.factory() as session:
            saved = await session.get(FeishuDelivery, row.task_id)
            assert saved is not None
            saved.failures, saved.retry_at, saved.last_error = 0, None, None
            await streams.mark_pushed(session, row.task_id, row.version)
            if row.is_complete:
                await self.enqueue_cards(session, row, compiled)
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
        units = await asyncio.to_thread(stream_units, answer)
        # 还没有可显示的正文（只有思考、标题栏或半行围栏）：保持占位，别把 answer 删掉。
        if units and not state.get("paused"):
            layout = state["units"] if "units" in state else list(INITIAL_LAYOUT)
            shell = stream_card(thinking, "", session_url=row.session_url, heading=heading)
            shell["body"]["elements"] = shell["body"]["elements"][:1]
            steps = plan(
                layout,
                units,
                anchor="thinking_panel",
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
        panel = shell["body"]["elements"][0]
        shell["body"]["elements"] = [panel]
        steps = plan(
            [],
            units,
            anchor="thinking_panel",
            shown=len(card_json(shell).encode()),
            show=remote_images_as_links,
        )
        elements = [e for action in steps.actions for e in action["params"].get("elements") or []]
        shell["body"]["elements"] = [panel, *elements]
        shell["config"]["streaming_mode"] = True
        await self.put_card(row.task_id, card_id, shell)
        steps.actions, steps.texts = [], []
        return steps

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
    ) -> Compiled:
        """终稿编译成若干张卡；关掉富卡片或编译自检不过时用简化卡。"""
        prefix = [thinking_panel(thinking, session_url=session_url, heading=heading)]
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
            )
            if not compiled.problems:
                return compiled
            log.warning("feishu_rich_card_invalid", problems=compiled.problems[:5])
        return await self.plain_cards(thinking, answer, session_url, heading, localized=True)

    async def plain_cards(
        self,
        thinking: str,
        answer: str,
        session_url: str | None,
        heading: str,
        *,
        localized: bool = False,
    ) -> Compiled:
        prefix = [thinking_panel(thinking, session_url=session_url, heading=heading)]
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
        if item.payload.get("_human_collaboration_id"):
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
            return
        if item.kind not in {"send", "welcome"}:
            raise FeishuError(-2, "unsupported outbox kind")
        chat = str(item.target.get("chat_id") or "")
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
        elif isinstance(item.payload.get("markdown"), str) and not item.payload.get("_plain"):
            # Markdown 消息（定时任务结果等）也优先用卡片；某张被拒只把那一张改发成 post。
            # 这些不是提问人自己的对话回复，回复按钮只显示成文字。
            await self.send_markdown_cards(chat, item.payload["markdown"], key=f"outbox:{item.id}")
        else:
            text = str(item.payload.get("markdown") or item.payload.get("text") or "…")
            await self.send_posts(chat, text, f"outbox:{item.id}")

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
                log.warning("feishu_rich_card_invalid", problems=compiled.problems[:5])
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
