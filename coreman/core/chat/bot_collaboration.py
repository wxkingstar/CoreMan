"""Single-hop bot help. Platform events, never an internal reply shortcut, dispatch peers."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    BotCollaboration,
    BotCollaborationRoute,
    ChatSession,
    InboundEvent,
    OutboxItem,
    Task,
)
from coreman.core.prompting import Speaker

AAD = "bot_collaboration.task_capability"
ACTIVE = ("requested", "waiting_helper", "helper_running", "waiting_source", "resuming")


def issue_capability(
    cipher: Cipher, *, task_id: int, user_id: str, now: float | None = None
) -> str:
    return cipher.encrypt(
        json.dumps(
            {"task": task_id, "actor": user_id, "exp": (time.time() if now is None else now) + 1800}
        ),
        AAD,
    )


def read_capability(cipher: Cipher, token: str, *, now: float | None = None) -> tuple[int, str]:
    data = json.loads(cipher.decrypt(token, AAD))
    if data["exp"] <= (time.time() if now is None else now):
        raise ValueError("expired capability")
    return int(data["task"]), str(data["actor"])


def event_matches(
    raw: dict[str, Any],
    *,
    tenant: str,
    chat: str,
    sender_union: str,
    message_id: str,
    parent_id: str | None = None,
) -> bool:
    ev = raw.get("event") or {}
    sender, msg = ev.get("sender") or {}, ev.get("message") or {}
    return bool(
        tenant
        and chat
        and sender_union
        and message_id
        and (raw.get("header") or {}).get("tenant_key") == tenant
        and sender.get("sender_type") == "bot"
        and (sender.get("sender_id") or {}).get("union_id") == sender_union
        and msg.get("chat_id") == chat
        and msg.get("chat_type") == "group"
        and msg.get("message_type") == "text"
        and msg.get("message_id") == message_id
        and (parent_id is None or msg.get("parent_id") == parent_id)
    )


async def routes_for(
    session: AsyncSession, bot_id: uuid.UUID, chat_id: str
) -> list[BotCollaborationRoute]:
    return list(
        await session.scalars(
            select(BotCollaborationRoute).where(
                BotCollaborationRoute.source_bot_id == bot_id,
                BotCollaborationRoute.chat_id == chat_id,
                BotCollaborationRoute.enabled.is_(True),
            )
        )
    )


async def authorized(
    session: AsyncSession, route: BotCollaborationRoute, pid: str, uid: uuid.UUID
) -> Speaker:
    if not route.enabled or route.source_bot_id == route.target_bot_id:
        raise ValueError("collaboration route disabled")
    speaker = await resolve_speaker(session, platform="feishu", platform_user_id=pid)
    if not speaker.known or speaker.user_id != uid:
        raise ValueError("original human identity changed")
    for bid in (route.source_bot_id, route.target_bot_id):
        bot = await session.get(Bot, bid, populate_existing=True)
        if bot is None or not bot.enabled or bot.platform != "feishu":
            raise ValueError("collaboration bot disabled")
        allowed = set(
            await session.scalars(
                select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bid)
            )
        )
        if allowed and uid not in allowed:
            raise ValueError("original human cannot use peer")
    return speaker


async def request_help(
    session: AsyncSession, *, task_id: int, actor: str, target_key: str, question: str
) -> BotCollaboration:
    task = await session.get(Task, task_id, with_for_update=True)
    if (
        not task
        or task.kind != "chat"
        or task.status not in tasks.ACTIVE
        or task.cancel_requested_at
        or task.payload.get("collaboration_id")
    ):
        raise ValueError("task cannot delegate")
    source = await session.get(InboundEvent, task.inbound_event_id)
    if (
        not source
        or source.platform != "feishu"
        or source.chat_type != "group"
        or (source.payload.get("sender") or {}).get("sender_type", "user") != "user"
    ):
        raise ValueError("human group task required")
    pid = source.sender_platform_user_id or ""
    speaker = await resolve_speaker(session, platform="feishu", platform_user_id=pid)
    if speaker.user_id is None or str(speaker.user_id) != actor:
        raise ValueError("capability actor mismatch")
    route = await session.scalar(
        select(BotCollaborationRoute)
        .join(Bot, Bot.id == BotCollaborationRoute.target_bot_id)
        .where(
            BotCollaborationRoute.source_bot_id == task.bot_id,
            BotCollaborationRoute.chat_id == source.chat_id,
            BotCollaborationRoute.enabled.is_(True),
            Bot.bot_key == target_key,
        )
    )
    if route is None:
        raise ValueError("peer not configured for this group")
    await authorized(session, route, pid, speaker.user_id)
    existing = await session.scalar(
        select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
    )
    if existing:
        if existing.route_id != route.id or existing.question != question:
            raise ValueError("one help request per task")
        return existing
    if not question.strip():
        raise ValueError("empty help question")
    info = await session.get(ChatSession, (task.bot_id, task.session_key))
    bot = await session.get(Bot, task.bot_id)
    if info is None or bot is None or not bot.relay_server_id:
        raise ValueError("source session unavailable")
    row = BotCollaboration(
        route_id=route.id,
        source_task_id=task.id,
        origin_user_id=speaker.user_id,
        origin_platform_user_id=pid,
        source_session_key=task.session_key,
        source_relay_session_id=info.relay_session_id,
        source_relay_id=bot.relay_server_id,
        question=question,
        status="requested",
        expires_at=datetime.now(UTC) + timedelta(seconds=max(30, min(route.timeout_seconds, 1800))),
    )
    session.add(row)
    await session.flush()
    return row


async def send_text(
    session: AsyncSession,
    row: BotCollaboration,
    route: BotCollaborationRoute,
    *,
    response: bool = False,
) -> None:
    source = await session.get(Task, row.source_task_id)
    assert source is not None
    origin = await session.get(InboundEvent, source.inbound_event_id)
    assert origin is not None
    request_item = await session.get(OutboxItem, row.request_outbox_id) if response else None
    if response and (request_item is None or not request_item.payload.get("_feishu_message_id")):
        raise ValueError("request delivery not confirmed")
    reply_mid = (
        str(request_item.payload["_feishu_message_id"]) if request_item else origin.platform_msg_id
    )
    mention = route.source_open_id if response else route.target_open_id
    text = escape((row.response if response else row.question) or "", quote=False)
    item = await outbox.add(
        session,
        bot_id=route.target_bot_id if response else route.source_bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"collaboration:{row.id}:{'reply' if response else 'ask'}",
        target={
            "chat_id": route.chat_id,
            "message_id": reply_mid,
        },
        payload={
            "text": f'<at user_id="{escape(mention, quote=True)}">协作伙伴</at>\n{text}',
            "_collaboration_id": str(row.id),
            "_collaboration_phase": "reply" if response else "ask",
        },
    )
    if item:
        if response:
            row.response_outbox_id, row.status = item.id, "waiting_source"
        else:
            row.request_outbox_id, row.status = item.id, "waiting_helper"


async def close(session: AsyncSession, row: BotCollaboration, status: str, error: str) -> None:
    if row.status not in ACTIVE:
        return
    row.status, row.error = status, error
    for tid in (row.source_task_id, row.helper_task_id, row.resume_task_id):
        if tid:
            await tasks.request_cancel(session, tid, error)
    for oid in (row.request_outbox_id, row.response_outbox_id):
        item = await session.get(OutboxItem, oid) if oid else None
        if item and item.status == "pending":
            await outbox.mark_skipped(session, item.id, error)
    route = await session.get(BotCollaborationRoute, row.route_id)
    assert route is not None
    source = await session.get(Task, row.source_task_id)
    assert source is not None
    origin = await session.get(InboundEvent, source.inbound_event_id)
    assert origin is not None
    await outbox.add(
        session,
        bot_id=route.source_bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"collaboration:{row.id}:closed",
        target={"chat_id": route.chat_id, "message_id": origin.platform_msg_id},
        payload={"text": f"本次协作已停止：{error}。未获得完整反馈，不能给出完成结论。"},
    )


async def tick(session: AsyncSession, now: datetime) -> int:
    """Reconcile real receipts after either commit order; row locks provide replay safety."""
    rows = list(
        await session.scalars(
            select(BotCollaboration)
            .where(BotCollaboration.status.in_(ACTIVE))
            .order_by(BotCollaboration.created_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    )
    count = 0
    for row in rows:
        route = await session.get(BotCollaborationRoute, row.route_id, populate_existing=True)
        assert route is not None
        try:
            await authorized(session, route, row.origin_platform_user_id, row.origin_user_id)
        except ValueError as exc:
            await close(session, row, "cancelled", str(exc))
            continue
        if row.expires_at <= now:
            await close(session, row, "timed_out", "等待协作伙伴反馈超时")
            continue
        if row.status in ("helper_running", "resuming"):
            running = await session.get(
                Task, row.helper_task_id if row.status == "helper_running" else row.resume_task_id
            )
            if running and running.status in ("failed", "cancelled", "timed_out"):
                await close(session, row, "failed", "协作任务中断")
            continue
        if row.status not in ("waiting_helper", "waiting_source"):
            continue
        reply = row.status == "waiting_source"
        item = await session.get(
            OutboxItem, row.response_outbox_id if reply else row.request_outbox_id
        )
        if item is None or item.status in ("failed", "skipped"):
            await close(session, row, "failed", "协作消息投递失败")
            continue
        mid = item.payload.get("_feishu_message_id")
        if not mid:
            continue
        receiver = route.source_bot_id if reply else route.target_bot_id
        event = await session.scalar(
            select(InboundEvent).where(
                InboundEvent.bot_id == receiver, InboundEvent.platform_msg_id == mid
            )
        )
        if event is None:
            continue
        request_item = await session.get(OutboxItem, row.request_outbox_id)
        assert request_item is not None
        if not event.payload.get("mentions_bot") or not event_matches(
            event.payload.get("raw") or {},
            tenant=route.tenant_key,
            chat=route.chat_id,
            sender_union=route.target_union_id if reply else route.source_union_id,
            message_id=mid,
            parent_id=request_item.payload.get("_feishu_message_id") if reply else None,
        ):
            await close(session, row, "failed", "协作消息身份或关联校验失败")
            continue
        task = await tasks.enqueue(
            session,
            tasks.NewTask(
                bot_id=receiver,
                kind="chat",
                user_id=row.origin_user_id,
                inbound_event_id=event.id,
                session_key=row.source_session_key if reply else f"collaboration:{row.id}",
                dedupe_key=f"collaboration:{row.id}:{'resume' if reply else 'helper'}",
                payload={
                    "collaboration_id": str(row.id),
                    "collaboration_phase": "resume" if reply else "helper",
                },
            ),
        )
        if task:
            if reply:
                row.resume_task_id, row.status = task.id, "resuming"
            else:
                row.helper_task_id, row.status = task.id, "helper_running"
            count += 1
    return count
