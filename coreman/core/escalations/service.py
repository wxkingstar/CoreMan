"""人工求助状态机；短事务内串行裁决，外部通知统一入 outbox。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from coreman.core.bus import outbox
from coreman.core.cron.delivery import chunks
from coreman.core.db.models import Bot, Escalation, OutboxItem, PlatformApp, User, UserIdentity
from coreman.core.errors import ApiError, not_found
from coreman.core.i18n.messages import msg

ACTIVE = ("pending", "replied")
OPEN = (*ACTIVE, "queued")
TERMINAL = ("completed", "expired", "cancelled")
# 通知入不了队（接收人账号已变更、应用失效）时 poll 返回的原因。
NOTIFICATION_UNAVAILABLE = "notification_unavailable"


def question_key(row: Escalation) -> str:
    """本轮「等对方回答」的那条通知（提问或追问）首片的出站幂等键。"""
    phase = "ask" if row.rounds == 0 else "followup"
    return f"escalation:{row.escalation_id}:{phase}:{row.rounds}:0"


def _question_key_sql() -> ColumnElement[str]:
    """与 `question_key` 同义的 SQL 表达式，走 outbox.dedupe_key 的唯一索引。"""
    phase = case((Escalation.rounds == 0, "ask"), else_="followup")
    return func.concat(
        "escalation:", Escalation.escalation_id, ":", phase, ":", Escalation.rounds, ":0"
    )


def is_expired(row: Escalation, now: datetime) -> bool:
    return row.expires_at <= now or bool(
        row.last_polled_at and now - row.last_polled_at > timedelta(seconds=300)
    )


async def lock(session: AsyncSession) -> None:
    # 求助生命周期变更只涉及少量行、无外部 IO。跨接收人的 group 也有固定全局锁序。
    await session.execute(text("SELECT pg_advisory_xact_lock(721309130009)"))


async def notify(session: AsyncSession, row: Escalation, content: str, phase: str) -> bool:
    """按接收人快照入队通知；接收人账号已变更或应用失效、入不了队时返回 False。"""
    platform = "feishu" if row.notify_platform == "feishu_bot" else "wecom"
    ident = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == row.to_user_id, UserIdentity.platform == platform
        )
    )
    if (
        ident is None
        or ident.platform_user_id != row.to_platform_user_id
        or row.platform_app_id is None
    ):
        return False
    for index, part in enumerate(chunks(content, 2048)):
        await outbox.add(
            session,
            bot_id=None,
            platform=platform,
            kind="notify",
            dedupe_key=f"escalation:{row.escalation_id}:{phase}:{row.rounds}:{index}",
            target={
                "user_id": str(row.to_user_id),
                "platform_user_id": ident.platform_user_id,
                "platform_app_id": str(row.platform_app_id),
                "escalation_id": row.escalation_id,
                "escalation_phase": phase,
                "escalation_round": row.rounds,
            },
            payload={
                "content": content if len(content.encode()) <= 2048 else part,
                "msgtype": "text",
            },
        )
    return True


async def notify_localized(
    session: AsyncSession, row: Escalation, key: str, phase: str, **values: object
) -> bool:
    recipient = await session.get(User, row.to_user_id)
    locale = recipient.locale if recipient else "zh"
    values["reply_hint"] = msg(
        "esc_quote_reply" if row.notify_platform == "feishu_bot" else "esc_direct_reply", locale
    )
    if "sender" in values:
        values["sender"] = (
            msg("esc_sender", locale, name=values["sender"]) if values["sender"] else ""
        )
    return await notify(session, row, msg(key, locale, **values), phase)


async def activate_next(session: AsyncSession, recipient: uuid.UUID, now: datetime) -> None:
    if await session.scalar(
        select(Escalation.id)
        .where(Escalation.to_user_id == recipient, Escalation.status.in_(ACTIVE))
        .limit(1)
    ):
        return
    row = await session.scalar(
        select(Escalation)
        .where(
            Escalation.to_user_id == recipient,
            Escalation.status == "queued",
            Escalation.expires_at > now,
        )
        .order_by(Escalation.id)
        .limit(1)
        .with_for_update()
    )
    if row is None:
        return
    if row.last_polled_at and now - row.last_polled_at > timedelta(seconds=300):
        row.status, row.resolution = "expired", "expired"
        await session.flush()
        await activate_next(session, recipient, now)
        return
    row.status = "pending"
    row.activated_at = now
    # 排队消耗墙钟期限；激活不无限延长已经存在的请求。
    bot = await session.get(Bot, row.bot_id)
    sender = await session.get(User, row.from_user_id) if row.from_user_id else None
    queued = await notify_localized(
        session,
        row,
        "esc_ask",
        "ask",
        bot=bot.name if bot else "CoreMan",
        sender=sender.display_name if sender else None,
        question=row.question,
    )
    await session.flush()
    if not queued:
        # 提问根本发不出去：留着 pending 只会让 Agent 空等到过期，直接结束并激活下一个。
        row.status, row.updated_at = "cancelled", now
        await session.flush()
        await activate_next(session, recipient, now)


async def create(
    session: AsyncSession,
    *,
    client_key: str,
    bot: Bot,
    sender: User | None,
    recipients: list[User],
    app: PlatformApp,
    question: str,
    request_id: str | None,
    now: datetime,
) -> list[Escalation]:
    await lock(session)
    group = (
        uuid.uuid5(uuid.NAMESPACE_URL, f"coreman-escalation:{client_key}:{request_id}").hex
        if request_id
        else uuid.uuid4().hex
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "bot": str(bot.id),
                "sender": str(sender.id) if sender else None,
                "recipients": sorted(str(user.id) for user in recipients),
                "question": question,
                "app": str(app.id),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    existing = list(
        await session.scalars(
            select(Escalation)
            .where(Escalation.owner_client_key == client_key, Escalation.group_id == group)
            .order_by(Escalation.id)
        )
    )
    if existing:
        if any(row.request_fingerprint != fingerprint for row in existing):
            raise ApiError(409, 409, "request_id 已用于不同的求助内容")
        return existing
    identities = {
        uid: platform_id
        for uid, platform_id in (
            await session.execute(
                select(UserIdentity.user_id, UserIdentity.platform_user_id).where(
                    UserIdentity.platform == app.platform,
                    UserIdentity.user_id.in_(
                        [u.id for u in recipients] + ([sender.id] if sender else [])
                    ),
                )
            )
        ).all()
    }
    rows: list[Escalation] = []
    for user in sorted(recipients, key=lambda u: str(u.id)):
        pending = await session.scalar(
            select(func.count())
            .select_from(Escalation)
            .where(Escalation.to_user_id == user.id, Escalation.status.in_(OPEN))
        )
        if pending is not None and pending >= 50:
            raise ApiError(429, 429, "接收人的待处理求助过多，请稍后再试")
        row = Escalation(
            escalation_id=uuid.uuid4().hex,
            owner_client_key=client_key,
            request_fingerprint=fingerprint,
            from_platform_user_id=identities.get(sender.id) if sender else None,
            to_platform_user_id=identities[user.id],
            group_id=group,
            bot_id=bot.id,
            from_user_id=sender.id if sender else None,
            to_user_id=user.id,
            notify_platform="feishu_bot" if app.platform == "feishu" else "wecom_app",
            platform_app_id=app.id,
            question=question,
            status="queued",
            last_polled_at=now,
            expires_at=now + timedelta(seconds=1800),
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    for row in rows:
        await activate_next(session, row.to_user_id, now)
    return rows


async def load(session: AsyncSession, identity: str, client_key: str) -> list[Escalation]:
    await lock(session)
    rows = list(
        await session.scalars(
            select(Escalation)
            .where(
                Escalation.owner_client_key == client_key,
                or_(Escalation.escalation_id == identity, Escalation.group_id == identity),
            )
            .order_by(Escalation.id)
            .with_for_update()
        )
    )
    if not rows:
        raise not_found("求助不存在或不属于此调用方")
    return rows


async def close(
    session: AsyncSession,
    row: Escalation,
    status: str,
    resolution: str | None,
    now: datetime,
    *,
    activate: bool = True,
) -> None:
    if row.status not in OPEN:
        return
    row.status, row.resolution, row.updated_at = status, resolution, now
    delivered = await session.scalar(
        select(OutboxItem.id).where(
            OutboxItem.dedupe_key == f"escalation:{row.escalation_id}:ask:0:0",
            OutboxItem.status == "sent",
        )
    )
    if row.activated_at is not None and delivered is not None:
        key = "esc_" + status
        if status == "expired":
            key += "_replied" if row.replies else "_empty"
        await notify_localized(session, row, key, status)
    await session.flush()
    if activate:
        await activate_next(session, row.to_user_id, now)


async def followup(session: AsyncSession, row: Escalation, question: str, now: datetime) -> None:
    if (
        row.status not in ACTIVE
        or is_expired(row, now)
        or row.rounds >= 2
        or not any(reply.get("round") == row.rounds for reply in row.replies)
    ):
        raise ApiError(409, 409, "只有收到本轮回复后才能追问，且最多追问两轮")
    row.rounds += 1
    row.followup_questions = [*row.followup_questions, question]
    row.status = "pending"
    row.last_polled_at = now
    row.updated_at = now
    if not await notify_localized(
        session, row, "esc_followup", "followup", round=row.rounds, question=question
    ):
        await close(session, row, "cancelled", None, now)


async def add_reply(
    session: AsyncSession,
    *,
    user: User,
    app: PlatformApp,
    content: str,
    msg_id: str,
    created_at: datetime,
    now: datetime,
    media: dict[str, str] | None = None,
    parent_message_id: str | None = None,
) -> Escalation | None:
    await lock(session)
    row = await session.scalar(
        select(Escalation)
        .where(
            Escalation.to_user_id == user.id,
            Escalation.platform_app_id == app.id,
            Escalation.status.in_(ACTIVE),
        )
        .with_for_update()
    )
    if (
        row is None
        or is_expired(row, now)
        or row.activated_at is None
        or created_at < row.activated_at.replace(microsecond=0)
    ):
        return None
    # 每轮通知都须已送达；追问还在出站队列时不能把旧回复记入新一轮。
    phase = "ask" if row.rounds == 0 else "followup"
    sent = await session.scalar(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"escalation:{row.escalation_id}:{phase}:{row.rounds}:0",
            OutboxItem.status == "sent",
        )
    )
    ident = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user.id, UserIdentity.platform == app.platform
        )
    )
    if (
        sent is None
        or (sent.sent_at and created_at < sent.sent_at.replace(microsecond=0))
        or ident is None
        or ident.platform_user_id != row.to_platform_user_id
        or any(reply.get("message_id") == msg_id for reply in row.replies)
    ):
        return None
    if app.platform == "feishu" and parent_message_id:
        quoted = await session.scalar(
            select(OutboxItem.id).where(
                OutboxItem.status == "sent",
                OutboxItem.target["escalation_id"].astext == row.escalation_id,
                OutboxItem.target["escalation_phase"].astext.in_([phase, "nudge1", "nudge2"]),
                OutboxItem.target["escalation_round"].as_integer() == row.rounds,
                OutboxItem.payload["_feishu_message_id"].astext == parent_message_id,
            )
        )
        if quoted is None:
            return None
    if len(row.replies) >= 100:
        raise ApiError(429, 429, "求助回复数量超过限制")
    row.replies = [
        *row.replies,
        {
            "content": content,
            "replied_at": now.isoformat(),
            "round": row.rounds,
            "message_id": msg_id,
            **({"media": media} if media else {}),
        },
    ]
    row.last_reply_at, row.updated_at = now, now
    row.status = "replied"
    # 同组首个有效回复者胜出，其它求助终止；不等进程内缓存/聚合窗口。
    siblings = list(
        await session.scalars(
            select(Escalation)
            .where(
                Escalation.group_id == row.group_id,
                Escalation.id != row.id,
                Escalation.status.in_(OPEN),
            )
            .order_by(Escalation.id)
            .with_for_update()
        )
    )
    for sibling in siblings:
        await close(session, sibling, "cancelled", None, now, activate=False)
    if not media and content.strip().lower() in {
        "完成",
        "over",
        "已处理",
        "知道了",
        "ok",
        "対応済み",
        "完了",
        "了解",
    }:
        await close(session, row, "completed", "offline", now, activate=False)
    await session.flush()
    for recipient in {s.to_user_id for s in siblings} | {row.to_user_id}:
        await activate_next(session, recipient, now)
    return row


async def delivery_failures(session: AsyncSession, rows: list[Escalation]) -> dict[str, str]:
    """因本轮提问 / 追问通知发不出去而结束的求助 → 原因（escalation_id 为键）。

    原因不另存列：由那条通知的出站记录推出来——`failed` 取其脱敏后的 last_error；整条没入队
    （activated 之后本该必有）记 `notification_unavailable`。被 Agent 取消、同组他人胜出的
    求助，其通知要么已发出、要么发送前重查被标 skipped，都不会被误判成发送失败。
    """
    candidates = {
        question_key(row): row
        for row in rows
        if row.status == "cancelled" and row.activated_at is not None
    }
    if not candidates:
        return {}
    items = {
        item.dedupe_key: item
        for item in await session.scalars(
            select(OutboxItem).where(OutboxItem.dedupe_key.in_(list(candidates)))
        )
    }
    failures: dict[str, str] = {}
    # 已发出的出站记录保留 7 天就会被清理：更早结束的求助查不到记录不代表没入队，不下结论。
    recent = datetime.now(UTC) - timedelta(days=6)
    for key, row in candidates.items():
        item = items.get(key)
        if item is None:
            if row.updated_at is not None and row.updated_at >= recent:
                failures[row.escalation_id] = NOTIFICATION_UNAVAILABLE
        elif item.status == "failed":
            failures[row.escalation_id] = item.last_error or "notification_failed"
    return failures


async def tick(session: AsyncSession, now: datetime) -> int:
    await lock(session)
    active = aliased(Escalation)
    active_for_recipient = (
        select(active.id)
        .where(active.to_user_id == Escalation.to_user_id, active.status.in_(ACTIVE))
        .exists()
    )
    # 本轮提问 / 追问的通知已判永久失败（重试耗尽或平台拒绝）：对方不会收到，求助不能继续挂着。
    question_failed = (
        select(OutboxItem.id)
        .where(OutboxItem.dedupe_key == _question_key_sql(), OutboxItem.status == "failed")
        .exists()
    )
    due = or_(
        and_(Escalation.status == "pending", question_failed),
        Escalation.expires_at <= now,
        Escalation.last_polled_at < now - timedelta(seconds=300),
        and_(Escalation.status == "queued", ~active_for_recipient),
        and_(
            Escalation.status == "pending",
            Escalation.replies == [],
            or_(
                and_(
                    Escalation.nudge_stage < 1,
                    Escalation.activated_at <= now - timedelta(seconds=480),
                ),
                and_(
                    Escalation.nudge_stage < 2,
                    Escalation.activated_at <= now - timedelta(seconds=1500),
                ),
            ),
        ),
    )
    rows = list(
        await session.scalars(
            select(Escalation)
            .where(Escalation.status.in_(OPEN), due)
            .order_by(Escalation.id)
            .limit(500)
            .with_for_update()
        )
    )
    failed_keys = set(
        await session.scalars(
            select(OutboxItem.dedupe_key).where(
                OutboxItem.dedupe_key.in_([question_key(row) for row in rows]),
                OutboxItem.status == "failed",
            )
        )
    )
    changed = 0
    for row in rows:
        if row.status == "pending" and question_key(row) in failed_keys:
            # 通知发不出去先于到期判定：poll 能拿到明确的失败原因，而不是笼统的 expired。
            await close(session, row, "cancelled", None, now, activate=False)
            changed += 1
        elif row.expires_at <= now or (
            row.last_polled_at and now - row.last_polled_at > timedelta(seconds=300)
        ):
            await close(session, row, "expired", "expired", now, activate=False)
            changed += 1
        elif row.status in ACTIVE and not row.replies and row.activated_at:
            elapsed = (now - row.activated_at).total_seconds()
            stage = 2 if elapsed >= 1500 else 1 if elapsed >= 480 else 0
            if stage > row.nudge_stage:
                row.nudge_stage = stage
                await notify_localized(
                    session,
                    row,
                    "esc_nudge_final" if stage == 2 else "esc_nudge",
                    f"nudge{stage}",
                    question=row.question[:60],
                )
                changed += 1
    await session.flush()
    for recipient in {row.to_user_id for row in rows}:
        await activate_next(session, recipient, now)
    return changed
