"""个人助理应用的出站：一个连接进程发送该应用下所有助理记录的回复与出站消息。

每条助理记录各用一个 `FeishuTransport`（出站锁、认领、流卡片都仍按记录），共用一个客户端和
一条守护连接；租约围栏换成应用的那一行。每轮先用一条查询找出此刻有活的记录，只对它们跑一轮，
空闲时的开销不随助理人数增长。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from coreman.core.db.models import AppLease, Bot, FeishuDelivery, OutboxItem, TaskStream
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost

log = get_logger(__name__)


class AppOutbound:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        client: FeishuClient,
        *,
        app: uuid.UUID,
        instance_id: str,
        generation: int,
        guard: AsyncConnection | None = None,
    ) -> None:
        self.factory, self.client = factory, client
        self.app, self.instance_id, self.generation = app, instance_id, generation
        self._guard = guard
        self._transports: dict[uuid.UUID, FeishuTransport] = {}

    async def fence(self) -> None:
        """应用的租约必须还在本实例这一代名下，且心跳没过期。"""
        async with self.factory() as session:
            row = await session.scalar(
                select(AppLease.app_id).where(
                    AppLease.app_id == self.app,
                    AppLease.holder_instance == self.instance_id,
                    AppLease.generation == self.generation,
                    AppLease.heartbeat_at > func.now() - timedelta(seconds=30),
                )
            )
        if row is None:
            raise LeaseLost

    async def due(self) -> list[tuple[uuid.UUID, bool]]:
        """此刻有活的助理记录及其富卡片开关。

        有活 = 有没推完的流式回复、有到点的待发出站、或有回复结束后还没撤掉的表情
        （对应 `FeishuTransport._deliver` 里的三段）。停用的记录不在内：它的出站留在队列里。
        只按归属、状态与时间筛，不读任何内容列；内容由各记录的 transport 取出时还原。
        """
        streaming = (
            select(TaskStream.task_id)
            .where(
                TaskStream.bot_id == Bot.id,
                TaskStream.delivery_mode == "stream",
                TaskStream.finish_pushed_at.is_(None),
            )
            .exists()
        )
        queued = (
            select(OutboxItem.id)
            .where(
                OutboxItem.bot_id == Bot.id,
                OutboxItem.status == "pending",
                OutboxItem.not_before <= func.now(),
            )
            .exists()
        )
        reactions = (
            select(FeishuDelivery.task_id)
            .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
            .where(
                TaskStream.bot_id == Bot.id,
                FeishuDelivery.reaction_done.is_(True),
                FeishuDelivery.reaction_id.is_not(None),
                or_(
                    FeishuDelivery.reaction_retry_at.is_(None),
                    FeishuDelivery.reaction_retry_at <= func.now(),
                ),
            )
            .exists()
        )
        stmt = (
            select(Bot.id, Bot.rich_cards)
            .where(
                Bot.platform_app_id == self.app,
                Bot.kind == "assistant",
                Bot.enabled.is_(True),
                or_(streaming, queued, reactions),
            )
            .order_by(Bot.id)
        )
        async with self.factory() as session:
            return [(row[0], bool(row[1])) for row in await session.execute(stmt)]

    def transport(self, bot_id: uuid.UUID) -> FeishuTransport:
        transport = self._transports.get(bot_id)
        if transport is None:
            transport = FeishuTransport(
                self.factory,
                self.client,
                bot_id=bot_id,
                instance_id=self.instance_id,
                generation=self.generation,
                guard=self._guard,
                lease_fence=self.fence,
            )
            self._transports[bot_id] = transport
        return transport

    async def round(self) -> bool:
        await self.fence()
        more = False
        for bot_id, rich_cards in await self.due():
            transport = self.transport(bot_id)
            transport.rich_cards = rich_cards
            try:
                more = await transport.round() or more
            except LeaseLost:
                raise
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", bot_id=str(bot_id), code=exc.code)
            except Exception as exc:  # noqa: BLE001 一个人的回复出问题不能卡住其他人
                # 只记异常类型：消息里可能带 SQL 参数或平台响应。
                log.error("feishu_app_round_failed", bot_id=str(bot_id), error=type(exc).__name__)
        return more
