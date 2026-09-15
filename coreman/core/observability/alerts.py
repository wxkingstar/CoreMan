"""事务化告警边沿，只有 scheduler leader 评估，通知复用持久出站箱。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.db.models import (
    AlertState,
    Bot,
    BotLease,
    OutboxItem,
    PlatformApp,
    RelayServer,
    Setting,
    Task,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg


async def emit(session: AsyncSession, row: AlertState, *, recovered: bool) -> None:
    setting = await session.get(Setting, "alert_channels")
    channels = setting.value.get("channels", []) if setting else []
    for channel in channels:
        app = await session.get(PlatformApp, uuid.UUID(channel["platform_app_id"]))
        user = await session.get(User, uuid.UUID(channel["user_id"]))
        if not app or not app.enabled or "notify" not in app.capabilities or not user:
            continue
        if (
            user.status != "active"
            or user.source == "bootstrap"
            or user.role not in {"platform_admin", "ai_committee"}
        ):
            continue
        identity = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id, UserIdentity.platform == app.platform
            )
        )
        if not identity:
            continue
        phase = "recovered" if recovered else "firing"
        content = (
            msg("alert_recovered" if recovered else "alert_firing", user.locale or "zh")
            + "\n"
            + msg(row.message_key, user.locale or "zh")
        )
        await outbox.add(
            session,
            bot_id=None,
            platform=app.platform,
            kind="notify",
            dedupe_key=f"alert:{row.key}:{row.generation}:{phase}:{app.id}:{user.id}",
            target={
                "platform_app_id": str(app.id),
                "user_id": str(user.id),
                "platform_user_id": identity.platform_user_id,
                "alert_key": row.key,
                "alert_generation": row.generation,
                "alert_recovered": recovered,
            },
            payload={"content": content, "msgtype": "text"},
        )


async def observe(
    session: AsyncSession,
    key: str,
    active: bool,
    message_key: str,
    now: datetime,
    *,
    delay: int = 0,
) -> bool:
    row = await session.get(AlertState, key, with_for_update=True)
    if row is None:
        row = AlertState(
            key=key,
            active=False,
            firing=False,
            generation=0,
            updated_at=now,
            message_key=message_key,
        )
        session.add(row)
    row.updated_at, row.message_key = now, message_key
    if not active:
        changed = row.firing
        if changed:
            await emit(session, row, recovered=True)
        row.active, row.firing, row.first_seen_at = False, False, None
        await session.flush()
        return changed
    if not row.active:
        row.active, row.first_seen_at = True, now
        row.generation += 1
    if not row.firing and row.first_seen_at and now - row.first_seen_at >= timedelta(seconds=delay):
        row.firing = True
        await emit(session, row, recovered=False)
        await session.flush()
        return True
    await session.flush()
    return False


async def tick(session: AsyncSession, now: datetime) -> None:
    # 失主瞬间重叠也只能有一个评估事务；限制查询和锁等待，不拖垮业务数据库。
    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(721309130017)")):
        return
    await session.execute(text("SET LOCAL statement_timeout = 2000"))
    queued, oldest = (
        await session.execute(
            select(func.count(), func.min(Task.run_after)).where(
                Task.status == "queued", Task.run_after <= now
            )
        )
    ).one()
    await observe(
        session,
        "queue",
        queued >= 100 or bool(oldest and now - oldest > timedelta(seconds=300)),
        "alert_queue",
        now,
        delay=300,
    )
    failed = await session.scalar(
        select(func.count())
        .select_from(OutboxItem)
        .where(OutboxItem.status == "failed", OutboxItem.target["alert_key"].astext.is_(None))
    )
    await observe(session, "outbox", bool(failed), "alert_outbox", now)
    recent = dict(
        (
            await session.execute(
                select(Task.status, func.count())
                .where(Task.finished_at > now - timedelta(minutes=10))
                .group_by(Task.status)
            )
        )
        .tuples()
        .all()
    )
    total = sum(recent.values())
    failures = recent.get("failed", 0) + recent.get("timed_out", 0)
    await observe(
        session,
        "task_failure",
        failures >= 5 and failures / max(total, 1) > 0.2,
        "alert_task_failure",
        now,
    )
    lost = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.finished_at > now - timedelta(minutes=10), Task.error_code == "worker_lost")
    )
    await observe(session, "reaper", bool(lost), "alert_reaper", now)
    seen = {"queue", "outbox", "task_failure", "reaper"}
    for identity, state in await session.execute(
        select(RelayServer.id, RelayServer.health_status).where(RelayServer.is_active)
    ):
        key = f"relay:{identity}"
        seen.add(key)
        await observe(session, key, state not in {"healthy", "unknown"}, "alert_relay", now)
    for identity, state, heartbeat in await session.execute(
        select(Bot.id, BotLease.connection_state, BotLease.heartbeat_at)
        .outerjoin(BotLease, BotLease.bot_id == Bot.id)
        .where(Bot.enabled)
    ):
        key = f"gateway:{identity}"
        seen.add(key)
        disconnected = (
            state != "subscribed" or heartbeat is None or now - heartbeat > timedelta(seconds=30)
        )
        await observe(
            session,
            key,
            disconnected,
            "alert_gateway",
            now,
            delay=0 if state in {"kicked", "auth_failed"} else 300,
        )
    # 删除/停用实例后清除旧告警，避免永久悬挂。
    for row in await session.scalars(
        select(AlertState).where(AlertState.active, AlertState.key.not_in(seen))
    ):
        await observe(session, row.key, False, row.message_key, now)
