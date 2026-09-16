"""租约限定的 CardKit 与出站投递，所有序号在调用平台前持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from coreman.core.bus import outbox, streams
from coreman.core.chat.reachability import private_target_valid
from coreman.core.db.models import Bot, BotLease, FeishuDelivery, OutboxItem, TaskStream
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.cards import (
    interaction_card,
    split_utf8,
    stream_card,
    visible_parts,
)

_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# 本 bot 的出站锁：同一时刻只有一个 child 能更新它的卡片、发它的出站条目。
_OUTPUT_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))")
# 常驻守护连接上用会话级锁：连接是 AUTOCOMMIT，不留未结束的事务，锁随连接关闭释放。
_OUTPUT_SESSION_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))")
# 一轮最多发这么多条出站；没发完就告诉调用方接着来，别等兜底轮询。
OUTBOX_PER_ROUND = 20


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
    ) -> None:
        self.factory, self.client = factory, client
        self.bot_id, self.instance_id, self.generation = bot_id, instance_id, generation
        self._last_call = 0.0
        # 常驻连接（子进程的应用独占锁连接，AUTOCOMMIT）：出站锁在它上面以会话级锁拿一次，
        # 持有到进程退出，每一轮就不必再占一条池连接跨越整轮。
        self._guard = guard
        self.credentials_fingerprint = credentials_fingerprint
        self._output_held = False
        # 这一轮开头已经查过租约围栏：轮内的平台调用不再逐次回表。
        self._round_fenced = False

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
        await asyncio.sleep(max(0, self._last_call + 0.3 - time.monotonic()))
        if not self._round_fenced:
            await self.fence()
        self._last_call = time.monotonic()
        return await self.client.call(method, path, **kwargs)

    async def send(
        self,
        chat_id: str,
        content: dict[str, Any],
        *,
        kind: str,
        key: str,
        reply_to: str | None = None,
    ) -> str:
        payload = json.dumps(content, ensure_ascii=False)
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
                params={"receive_id_type": "chat_id"},
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
            for row in rows:
                try:
                    await self.push(row)
                except FeishuError as exc:
                    async with self.factory() as session:
                        delivery = await session.get(FeishuDelivery, row.task_id)
                        assert delivery is not None
                        delivery.failures += 1
                        delivery.last_error = str(exc)
                        delivery.retry_at = datetime.now(UTC) + timedelta(
                            seconds=min(120, 2 ** min(delivery.failures, 7))
                        )
                        if delivery.failures >= 6 or exc.code in {230013, -2}:
                            delivery.fallback = True
                        await session.commit()
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
        if delivery.fallback or datetime.now(UTC) - delivery.created_at >= timedelta(days=13):
            if row.is_complete:
                async with self.factory() as session:
                    answer = row.final_text if row.final_text is not None else row.pending_text
                    await self.enqueue_final(session, row, visible_parts("", answer)[1], start=0)
                    await streams.mark_finish_pushed(session, row.task_id)
                    await session.commit()
            return
        if delivery.retry_at and delivery.retry_at > datetime.now(UTC):
            return
        if not delivery.card_id:
            result = await self.call(
                "POST",
                "/open-apis/cardkit/v1/cards",
                json={
                    "type": "card_json",
                    "data": json.dumps(stream_card("", ""), ensure_ascii=False),
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
        # 官方流式模式 10 分钟后自动关闭；提前关闭后仍可更新同一卡片。
        # https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview
        expired = datetime.now(UTC) - delivery.created_at >= timedelta(minutes=9)
        if (
            row.version <= row.pushed_version
            and not row.is_complete
            and (not expired or delivery.is_static)
        ):
            return
        answer = (
            row.final_text if row.is_complete and row.final_text is not None else row.pending_text
        )
        card = stream_card(row.thinking_md, answer, streaming=not (row.is_complete or expired))
        if (row.is_complete or expired) and not delivery.is_static:
            await self.call(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/settings",
                json={
                    "sequence": await self.sequence(row.task_id),
                    "settings": json.dumps({"config": {"streaming_mode": False}}),
                },
            )
            async with self.factory() as session:
                saved = await session.get(FeishuDelivery, row.task_id)
                assert saved is not None
                saved.is_static = True
                await session.commit()
            delivery.is_static = True
        if delivery.is_static:
            seq = await self.sequence(row.task_id)
            await self.call(
                "PUT",
                f"/open-apis/cardkit/v1/cards/{card_id}",
                json={
                    "sequence": seq,
                    "uuid": stable_uuid(f"card:{row.task_id}:{seq}"),
                    "card": {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
                },
            )
        else:
            elements = card["body"]["elements"]
            for element_id, content in [
                ("thinking", elements[0]["elements"][0]["content"]),
                ("answer", elements[1]["content"]),
            ]:
                await self.call(
                    "PUT",
                    f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
                    json={"sequence": await self.sequence(row.task_id), "content": content},
                )
        async with self.factory() as session:
            saved = await session.get(FeishuDelivery, row.task_id)
            assert saved is not None
            saved.failures, saved.retry_at, saved.last_error = 0, None, None
            await streams.mark_pushed(session, row.task_id, row.version)
            if row.is_complete:
                await self.enqueue_final(session, row, visible_parts("", answer)[1], start=1)
                await streams.mark_finish_pushed(session, row.task_id)
            await session.commit()

    async def enqueue_final(
        self, session: AsyncSession, row: TaskStream, answer: str, *, start: int
    ) -> None:
        for index, chunk in enumerate(split_utf8(answer)[start:], start):
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"{row.task_id}:overflow:{index}",
                target={"chat_id": row.reply_context.get("chat_id")},
                payload={"markdown": chunk},
            )
        if row.pending_card:
            await outbox.add(
                session,
                bot_id=row.bot_id,
                platform="feishu",
                kind="send",
                dedupe_key=f"{row.task_id}:card:0",
                target={"chat_id": row.reply_context.get("chat_id")},
                payload={"card": row.pending_card},
            )

    async def consume_one(self) -> bool:
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
                await self._send_item(item)
                await session.flush()
                await outbox.mark_sent(session, item.id)
            except FeishuError as exc:
                if exc.code in {230013, -2}:
                    await outbox.fail(session, item.id, str(exc))
                else:
                    await outbox.mark_failed(session, item.id, str(exc))
            await session.commit()
            return True

    async def _send_item(self, item: OutboxItem) -> None:
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
            mid = await self.send(
                chat, interaction_card(card), kind="interactive", key=f"outbox:{item.id}"
            )
            item.payload = {**item.payload, "_feishu_message_id": mid}
        else:
            for index, chunk in enumerate(
                split_utf8(str(item.payload.get("markdown") or item.payload.get("text") or "…"))
            ):
                await self.send(
                    chat,
                    {"zh_cn": {"title": "", "content": [[{"tag": "md", "text": chunk}]]}},
                    kind="post",
                    key=f"outbox:{item.id}:{index}",
                )
