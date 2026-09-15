"""outbox 读写（spec §5.4、§6.4）。

投递顺序与失败语义：

- 同一个任务的主动消息（`{task_id}:send:*`、`{task_id}:card:0`……）属于同一个投递组，组号由
  `dedupe_key` 的数字前缀推出，**不写进 payload**（payload 是要发给平台的内容，混进内部键
  迟早会漏出去）。
- 组内按 id 串行：前序项仍是 pending / sending（还可能成功）时，后续项被认领语句挡住，状态不变。
- 前序项永久失败（failed / skipped）后顺序保证就没有意义了，后续项立即释放可领——宁可少一段
  前文，也不能让「任务已完成」和卡片跟着一起静默消失。释放时记 WARNING + 失败指标。
- 结果未知（发出后没等到回执、网关在等回执时死掉）按普通失败有界重试：下次换新的
  `_attempt_id` 重发，接受极少量重复，拒绝静默丢失。「sending 超时」的回收由 scheduler 周期
  任务（`recover_abandoned`）做，不放在每秒一次的认领里。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus.notify import notify
from coreman.core.db.models import OutboxItem
from coreman.core.logging import get_logger
from coreman.core.observability.metrics import OUTBOX_FAILED, after_commit

BACKOFF_SECONDS = (2, 10, 30, 120, 120, 120)
MAX_ATTEMPTS = 6
# begin_attempt 之后这么久还停在 sending，就当网关在等回执时没了（回执超时 10 秒，留足余量）。
ATTEMPT_LEASE_SECONDS = 30
ABANDONED_ERROR = "delivery_unknown: gateway lost before acknowledgement"
_GROUP_PATTERN = re.compile(r"^([0-9]+):")
log = get_logger(__name__)


def delivery_group(kind: str, dedupe_key: str) -> str | None:
    """投递组号：`send` 且幂等键以 `{task_id}:` 开头时就是 task_id，否则不分组。"""
    if kind != "send":
        return None
    match = _GROUP_PATTERN.match(dedupe_key)
    return match.group(1) if match else None


def _group_sql(alias: str) -> str:
    """与 `delivery_group` 同义的 SQL 表达式。"""
    return (
        f"(CASE WHEN {alias}.kind = 'send' "
        f"THEN substring({alias}.dedupe_key from '^([0-9]+):') END)"
    )


# 外层与 NOT EXISTS 都只看 pending / sending 的行，走 `outbox_active_idx (bot_id, id)`。
_CLAIM_SQL = text(
    f"""
UPDATE outbox SET status='sending'
WHERE id = (SELECT q.id FROM outbox q
            WHERE q.bot_id=:bot_id AND q.status='pending' AND q.not_before<=now()
            AND NOT EXISTS (SELECT 1 FROM outbox prev
              WHERE prev.bot_id=q.bot_id AND prev.status IN ('pending','sending')
              AND prev.id<q.id AND {_group_sql("prev")} = {_group_sql("q")})
            ORDER BY q.id LIMIT 1 FOR UPDATE OF q SKIP LOCKED)
RETURNING id
"""
)
_RELEASED_SQL = text(
    f"""
SELECT f.dedupe_key, count(q.id) FROM outbox f
LEFT JOIN outbox q ON q.bot_id=f.bot_id AND q.status IN ('pending','sending')
  AND q.id>f.id AND {_group_sql("q")} = {_group_sql("f")}
WHERE f.id=:id AND {_group_sql("f")} IS NOT NULL
GROUP BY f.dedupe_key
"""
)


def public_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """发给平台之前剥掉下划线开头的内部键（`_attempt_id`、`_feishu_message_id`……）。"""
    return {k: v for k, v in (payload or {}).items() if not str(k).startswith("_")}


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
    """领一条到点的 pending；同组前序项还没落定（pending / sending）时跳过这一组。"""
    row = (await session.execute(_CLAIM_SQL, {"bot_id": bot_id})).first()
    return (
        None if row is None else await session.get(OutboxItem, int(row[0]), populate_existing=True)
    )


async def mark_sent(session: AsyncSession, item_id: int) -> None:
    """只认还没落定的行：迟到的回执不得把已判死 / 已跳过的条目改回 sent。

    通知消费者在行锁里直接从 pending 发出、不经过 sending，所以 pending 也算。
    """
    await session.execute(
        update(OutboxItem)
        .where(OutboxItem.id == item_id, OutboxItem.status.in_(("pending", "sending")))
        .values(status="sent", sent_at=func.now())
    )


async def _retry_or_fail(
    session: AsyncSession, item_id: int, error: str, *, only_sending: bool
) -> str | None:
    """attempts+1；未达上限回 pending 并退避，否则 failed。`only_sending` 且没命中返回 None。"""
    item = await session.get(OutboxItem, item_id, populate_existing=True)
    if item is None:
        raise LookupError(f"outbox {item_id} 不存在")
    if only_sending and item.status != "sending":
        return None
    attempts = item.attempts + 1
    values: dict[str, Any] = {"attempts": attempts, "last_error": error[:2000]}
    if attempts < MAX_ATTEMPTS:
        status = "pending"
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        values["not_before"] = func.now() + timedelta(seconds=delay)
    else:
        status = "failed"
    values["status"] = status
    stmt = update(OutboxItem).where(OutboxItem.id == item_id)
    if only_sending:
        stmt = stmt.where(OutboxItem.status == "sending")
    row = (await session.execute(stmt.values(**values).returning(OutboxItem.id))).first()
    if row is None:
        return None
    if status == "failed":
        await _note_permanent_failure(session, item_id, error)
    return status


async def mark_failed(session: AsyncSession, item_id: int, error: str) -> str:
    """attempts+1；未达上限置回 pending 并按 BACKOFF_SECONDS 退避，否则 failed。返回新状态。"""
    status = await _retry_or_fail(session, item_id, error, only_sending=False)
    if status is None:  # only_sending=False 时不会发生，留给类型检查
        raise LookupError(f"outbox {item_id} 不存在")
    return status


async def unknown_attempt(session: AsyncSession, item_id: int, detail: str) -> str | None:
    """发出去了却没等到回执：结果未知，按普通失败有界重试（下一次尝试换新 `_attempt_id`）。

    只认 sending：回执迟到时条目可能已经被 scheduler 回收过了。返回新状态，未命中返回 None。
    """
    return await _retry_or_fail(session, item_id, detail, only_sending=True)


async def fail(session: AsyncSession, item_id: int, error: str) -> None:
    """永久失败（重试也没用的平台错误码）：直接 failed，同组后续项随即释放。"""
    row = (
        await session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item_id, OutboxItem.status.in_(("pending", "sending")))
            .values(status="failed", attempts=OutboxItem.attempts + 1, last_error=error[:2000])
            .returning(OutboxItem.id)
        )
    ).first()
    if row is not None:
        await _note_permanent_failure(session, item_id, error)


async def _note_permanent_failure(session: AsyncSession, item_id: int, error: str) -> None:
    """记失败指标；同组还有后续项在排队的话，它们从此不再等这一条，留一条 WARNING。"""
    after_commit(session, OUTBOX_FAILED.inc)
    row = (await session.execute(_RELEASED_SQL, {"id": item_id})).first()
    if row is None or not row[1]:
        return
    dedupe_key, released = str(row[0]), int(row[1])
    after_commit(
        session,
        lambda: log.warning(
            "outbox_dependents_released",
            outbox_id=item_id,
            dedupe_key=dedupe_key,
            released=released,
            error=error[:200],
        ),
    )


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
        await _note_permanent_failure(session, item_id, error)
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
    """人工重投：仅 failed 可重投，attempts 归零并立即可领。

    同组还没发出去的后续项会重新排到它后面（认领语句看到它又是 pending 了）。
    """
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
    """发帧之前把这次尝试持久化：之后进程死掉，scheduler 会按结果未知回收它。"""
    item.payload = {**item.payload, "_attempt_id": req_id}
    item.not_before = datetime.now(UTC) + timedelta(seconds=ATTEMPT_LEASE_SECONDS)
    await session.commit()


async def recover_abandoned(session: AsyncSession, *, limit: int = 500) -> int:
    """scheduler 周期任务：回收网关在等回执时丢下的尝试（sending 且过了尝试租期）。

    飞书与通知消费者的 sending 只存在于未提交事务里（别的会话看到的仍是 pending），这里能看到的
    已提交 sending 只可能来自 `begin_attempt`。`SKIP LOCKED` 让开正在收尾的那一条。
    """
    ids = list(
        (
            await session.execute(
                select(OutboxItem.id)
                .where(OutboxItem.status == "sending", OutboxItem.not_before < func.now())
                .order_by(OutboxItem.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    recovered = 0
    for item_id in ids:
        if await _retry_or_fail(session, item_id, ABANDONED_ERROR, only_sending=True):
            recovered += 1
    return recovered
