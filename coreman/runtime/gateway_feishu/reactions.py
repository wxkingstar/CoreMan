"""Durable, best-effort typing lifecycle, independent of answer delivery retries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from coreman.core.db.models import FeishuDelivery, TaskStream
from coreman.core.platforms.feishu import FeishuError

if TYPE_CHECKING:
    from coreman.runtime.gateway_feishu.transport import FeishuTransport


async def typing(transport: FeishuTransport, task_id: int, *, done: bool = False) -> None:
    from coreman.runtime.gateway_feishu.transport import api_id

    async with transport.factory() as session:
        delivery = await session.get(FeishuDelivery, task_id)
        row = await session.get(TaskStream, task_id)
        if delivery is None or row is None:
            return
        if done:
            delivery.reaction_done = True
            await session.commit()
        mid = row.reply_context.get("message_id")
        if not mid or row.reply_context.get("_collaboration_helper"):
            return
        if delivery.reaction_done and not delivery.reaction_id:
            return
        if delivery.reaction_retry_at and delivery.reaction_retry_at > datetime.now(UTC):
            return
        try:
            base = f"/open-apis/im/v1/messages/{api_id(mid)}/reactions"
            if delivery.reaction_done:
                await transport.call("DELETE", f"{base}/{api_id(delivery.reaction_id)}")
                delivery.reaction_id = None
            elif not delivery.reaction_id:
                result = await transport.call(
                    "POST", base, json={"reaction_type": {"emoji_type": "Typing"}}
                )
                delivery.reaction_id = api_id((result.get("data") or {}).get("reaction_id"))
            delivery.reaction_failures = 0
            delivery.reaction_retry_at = None
        except FeishuError:
            # Missing permissions or a transient platform error must never block the answer.
            delivery.reaction_failures += 1
            delivery.reaction_retry_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2 ** min(delivery.reaction_failures, 9))
            )
        await session.commit()


async def clean_finished(transport: FeishuTransport) -> None:
    """Retry cleanup even after the stream has been marked fully delivered."""
    async with transport.factory() as session:
        ids = list(
            await session.scalars(
                select(FeishuDelivery.task_id)
                .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
                .where(
                    TaskStream.bot_id == transport.bot_id,
                    FeishuDelivery.reaction_done.is_(True),
                    FeishuDelivery.reaction_id.is_not(None),
                    or_(
                        FeishuDelivery.reaction_retry_at.is_(None),
                        FeishuDelivery.reaction_retry_at <= datetime.now(UTC),
                    ),
                )
                .limit(50)
            )
        )
    for task_id in ids:
        await typing(transport, task_id)
