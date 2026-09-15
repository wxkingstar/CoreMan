"""outbox 读写（spec §5.4、§6.4）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import OutboxItem
from coreman.core.observability.metrics import OUTBOX_FAILED, after_commit
from coreman.runtime.bus.notify import notify

BACKOFF_SECONDS = (2, 10, 30, 120, 120, 120)
MAX_ATTEMPTS = 6
_CLAIM_SQL = text(
    """
UPDATE outbox SET status='sending'
WHERE id = (SELECT q.id FROM outbox q
            WHERE q.bot_id=:bot_id AND q.status='pending' AND q.not_before<=now()
            AND NOT EXISTS (SELECT 1 FROM outbox prev WHERE prev.bot_id=q.bot_id
              AND prev.id<q.id AND prev.status IN ('pending','sending')
              AND prev.payload->>'_delivery_group'=q.payload->>'_delivery_group')
            ORDER BY q.id LIMIT 1 FOR UPDATE OF q SKIP LOCKED)
RETURNING id
"""
)


async def add(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID | None,
    platform: str,
    kind: str,
    dedupe_key: str,
    target: dict[str, Any],
    payload: dict[str, Any],
    lease_generation: int | None = None,
    not_before: datetime | None = None,
) -> OutboxItem | None:
    """幂等入队；同 dedupe_key 已存在时返回 None，不发通知。"""
    if (kind == "notify") != (bot_id is None):
        raise ValueError("通知项不绑定 bot；其它出站项必须绑定 bot")
    group = dedupe_key.split(":", 1)[0]
    if group.isdigit() and kind == "send":
        payload = {**payload, "_delivery_group": group}
    values: dict[str, Any] = {
        "bot_id": bot_id,
        "platform": platform,
        "kind": kind,
        "dedupe_key": dedupe_key,
        "target": target,
        "payload": payload,
        "lease_generation": lease_generation,
    }
    if not_before is not None:
        values["not_before"] = not_before
    stmt = (
        insert(OutboxItem)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[OutboxItem.dedupe_key])
    )
    row = (await session.execute(stmt.returning(OutboxItem.id))).first()
    if row is None:
        return None
    await notify(session, "outbox_added", {"bot_id": str(bot_id)})
    return await session.get(OutboxItem, int(row[0]), populate_existing=True)


async def claim_next(session: AsyncSession, *, bot_id: uuid.UUID) -> OutboxItem | None:
    # A committed attempt abandoned by a crashed gateway has an unknown outcome.
    # Do not silently retry it with a new platform request ID.
    await session.execute(
        update(OutboxItem)
        .where(
            OutboxItem.bot_id == bot_id,
            OutboxItem.status == "sending",
            OutboxItem.not_before < func.now(),
            OutboxItem.payload.has_key("_attempt_id"),
        )
        .values(status="failed", last_error="delivery_unknown: gateway lost before acknowledgement")
    )
    await session.execute(
        text("""
        UPDATE outbox q SET status='failed',
        last_error='dependency_failed: preceding delivery failed'
        WHERE q.bot_id=:bot_id AND q.status='pending' AND EXISTS (
          SELECT 1 FROM outbox prev WHERE prev.bot_id=q.bot_id AND prev.id<q.id
          AND prev.status='failed'
          AND prev.payload->>'_delivery_group'=q.payload->>'_delivery_group')
    """),
        {"bot_id": bot_id},
    )
    row = (await session.execute(_CLAIM_SQL, {"bot_id": bot_id})).first()
    return (
        None if row is None else await session.get(OutboxItem, int(row[0]), populate_existing=True)
    )


async def mark_sent(session: AsyncSession, item_id: int) -> None:
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id, OutboxItem.status == "sending")
        .values(status="sent", sent_at=func.now())
    )


async def mark_failed(session: AsyncSession, item_id: int, error: str) -> str:
    """attempts+1；未达上限置回 pending 并按 BACKOFF_SECONDS 退避，否则 failed。返回新状态。"""
    item = await session.get(OutboxItem, item_id, populate_existing=True)
    if item is None:
        raise LookupError(f"outbox {item_id} 不存在")
    attempts = item.attempts + 1
    values: dict[str, Any] = {"attempts": attempts, "last_error": error[:2000]}
    if attempts < MAX_ATTEMPTS:
        status = "pending"
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        values["not_before"] = func.now() + timedelta(seconds=delay)
    else:
        status = "failed"
    values["status"] = status
    await session.execute(update(OutboxItem).where(OutboxItem.id == item_id).values(**values))
    if status == "failed":
        after_commit(session, OUTBOX_FAILED.inc)
    return status


async def rejected(
    session: AsyncSession, item_id: int, *, error: str, retry_after: float | None
) -> str | None:
    """平台异步回了错误码：`sent` 是假的，这条消息其实没送到。

    `ws.send` 成功只代表「写进了连接」，企微几秒后才会用 errcode 告诉你它拒了（频控 846607
    最常见）。不把这条回滚成 pending 的话，后台推送这条长任务唯一的通道就静默丢消息。

    `retry_after=None` 表示这个错误码重试也没用（凭证、参数错），直接终态 failed。只认
    `status='sent'`：迟到的错误码不能把已经被人重投或已经判死的条目再拽回队列。返回新状态，
    没命中返回 None。
    """
    item = await session.get(OutboxItem, item_id, populate_existing=True)
    if item is None or item.status != "sent":
        return None
    attempts = item.attempts + 1
    values: dict[str, Any] = {"attempts": attempts, "last_error": error[:2000], "sent_at": None}
    if retry_after is not None and attempts < MAX_ATTEMPTS:
        values["status"] = "pending"
        values["not_before"] = func.now() + timedelta(seconds=max(retry_after, 0.0))
    else:
        values["status"] = "failed"
    row = (
        await session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item_id, OutboxItem.status == "sent")
            .values(**values)
            .returning(OutboxItem.id)
        )
    ).first()
    if row is not None and values["status"] == "failed":
        after_commit(session, OUTBOX_FAILED.inc)
    return None if row is None else str(values["status"])


async def defer(session: AsyncSession, item_id: int, seconds: float) -> None:
    """把已领取的条目放回队列并推迟 `seconds` 秒（频控用）。

    与 `mark_failed` 的区别：不算失败、不涨 attempts、不消耗重试额度——被平台频控挡下来
    只是「现在不能发」，不是「发不出去」。
    """
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id, OutboxItem.status == "sending")
        .values(status="pending", not_before=func.now() + timedelta(seconds=max(seconds, 0.0)))
    )


async def mark_skipped(session: AsyncSession, item_id: int, reason: str) -> None:
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id)
        .values(status="skipped", last_error=reason[:2000])
    )


async def retry(session: AsyncSession, item_id: int) -> bool:
    """人工重投：仅 failed 可重投，attempts 归零并立即可领。"""
    row = (
        await session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item_id, OutboxItem.status == "failed")
            .values(status="pending", attempts=0, not_before=func.now(), last_error=None)
            .returning(OutboxItem.bot_id)
        )
    ).first()
    if row is None:
        return False
    await notify(session, "outbox_added", {"bot_id": str(row[0])})
    return True


async def list_failed(session: AsyncSession, *, limit: int = 200) -> list[OutboxItem]:
    stmt = (
        select(OutboxItem)
        .where(OutboxItem.status == "failed")
        .order_by(OutboxItem.id.desc())
        .limit(limit)
    )
    return list(
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars()
    )


async def count_pending(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(OutboxItem).where(OutboxItem.status == "pending")
    return int((await session.execute(stmt)).scalar_one())


async def begin_attempt(session: AsyncSession, item: OutboxItem, req_id: str) -> None:
    item.payload = {**item.payload, "_attempt_id": req_id}
    item.not_before = datetime.now(UTC) + timedelta(seconds=30)
    await session.commit()


async def unknown_attempt(session: AsyncSession, item_id: int, detail: str) -> None:
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id, OutboxItem.status == "sending")
        .values(status="failed", last_error=detail[:2000])
    )
