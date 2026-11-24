"""业务系统代员工发起的任务里的写操作：确认卡发到员工私聊、谁能点、查询时的写操作状态。

API 这一轮没有人在飞书里打字，但发起人就是员工本人，结果本来就发在他和 AI 员工的私聊里：
写操作照常暂存，本轮结束时把确认卡发到这个私聊，只有他本人在这里点才算。见
docs/superpowers/specs/2026-11-24-api-tasks-system-clients.md 第 6.4 节。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    ApiTask,
    Bot,
    InboundEvent,
    InteractionState,
    SystemCall,
    Task,
)
from coreman.core.timeutils import utcnow


async def client_task(session: AsyncSession, task: Task) -> ApiTask | None:
    """`task` 是业务系统代员工发起的那一轮（或它的续接）时，返回对应的 API 任务。"""
    from coreman.core.api_tasks.service import client_session_key

    payload = task.payload or {}
    raw = payload.get("api_task_id")
    if not raw or not payload.get("api_client"):
        return None
    try:
        row = await session.get(ApiTask, uuid.UUID(str(raw)))
    except ValueError:
        return None
    if (
        row is None
        or row.client_key is None
        or row.bot_id != task.bot_id
        or task.session_key != client_session_key(row.id)
    ):
        return None
    return row


async def confirm_origin(
    session: AsyncSession, task: Task, user_id: uuid.UUID
) -> tuple[str, str] | None:
    """确认卡发往员工私聊：(chat_id, "single")；不是发起人本人的任务返回 None。"""
    from coreman.core.api_tasks.service import allowed_to_use

    row = await client_task(session, task)
    if row is None or row.user_id != user_id:
        return None
    bot = await session.get(Bot, task.bot_id)
    if bot is None or bot.platform != "feishu" or not bot.enabled:
        return None
    if not await allowed_to_use(session, bot, user_id):
        return None
    return row.chat_id, "single"


async def card_clicker(
    session: AsyncSession, origin: Task, inbound: InboundEvent
) -> tuple[uuid.UUID, str] | None:
    """(user_id, chat_id)：点卡的必须是发起人本人，在他和本 AI 员工的私聊里点。

    调用方另行核验卡片确实是我们发到这个会话的。飞书的卡片回调不带会话类型，私聊只认真实
    私聊消息留下的记录。
    """
    from coreman.core.chat.identity import resolve_speaker
    from coreman.core.chat.reachability import private_chat

    row = await client_task(session, origin)
    if row is None:
        return None
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
    )
    if not speaker.known or speaker.user_id != row.user_id:
        return None
    raw = inbound.payload.get("raw") or {}
    header, event = raw.get("header") or {}, raw.get("event") or {}
    operator, context = event.get("operator") or {}, event.get("context") or {}
    if (
        inbound.platform != "feishu"
        or inbound.kind != "card_action"
        or header.get("event_type") != "card.action.trigger"
        or inbound.chat_id != row.chat_id
        or context.get("open_chat_id") != row.chat_id
        or context.get("open_message_id") != inbound.reply_context.get("message_id")
        or not inbound.sender_open_id
        or operator.get("open_id") != inbound.sender_open_id
        or operator.get("user_id") != inbound.sender_platform_user_id
    ):
        return None
    if not await private_chat(session, origin.bot_id, row.user_id, row.chat_id):
        return None
    return row.user_id, row.chat_id


# 单个写操作在 API 查询里的状态。
_ITEM = {"ok": "succeeded", "failed": "failed", "unknown": "unknown", "sending": "unknown"}


def _batch_status(state: InteractionState, now: Any) -> str:
    if state.status == "open":
        lapsed = state.expires_at is not None and state.expires_at <= now
        return "expired" if lapsed and state.state.get("sent") else "pending_confirm"
    if state.status == "cancelled":
        return "rejected"
    if state.status == "expired":
        return "expired"
    return "confirmed"


async def view(session: AsyncSession, row: ApiTask) -> list[dict[str, Any]]:
    """这次任务（含确认后的续接轮）提出的写操作：业务系统、操作和状态，不含请求内容。"""
    from coreman.core.api_tasks.service import client_session_key
    from coreman.core.systems_catalog import batch, confirm

    task_ids = list(
        await session.scalars(
            select(Task.id)
            .where(Task.bot_id == row.bot_id, Task.session_key == client_session_key(row.id))
            .order_by(Task.id)
        )
    )
    if not task_ids:
        return []
    states = list(
        await session.scalars(
            select(InteractionState)
            .where(
                InteractionState.bot_id == row.bot_id,
                InteractionState.kind.in_((confirm.KIND, batch.KIND)),
                InteractionState.scope_key.in_([str(t) for t in task_ids]),
            )
            .order_by(InteractionState.created_at)
        )
    )
    proposed = list(
        await session.scalars(
            select(SystemCall)
            .where(SystemCall.task_id.in_(task_ids), SystemCall.outcome == "proposed")
            .order_by(SystemCall.id)
        )
    )
    now = utcnow()
    out: list[dict[str, Any]] = []
    for state in states:
        staged = state.state or {}
        status = _batch_status(state, now)
        if state.kind == batch.KIND:
            entry: dict[str, Any] = {
                "system": staged.get("system"),
                "operation": staged.get("operation_id"),
                "batch": True,
                "status": status,
            }
            if status == "confirmed":
                entry["items"] = dict(await batch.counts(session, state.id))
            out.append(entry)
            continue
        # 暂存时每一项记一行 proposed，顺序与暂存顺序一致。
        calls = [c for c in proposed if str(c.task_id) == state.scope_key]
        run = staged.get(confirm.RUN) or {}
        progress = [str(s) for s in run.get("progress") or []]
        for index, call in enumerate(calls):
            item_status = status
            if status == "confirmed":
                step = progress[index] if index < len(progress) else "pending"
                item_status = _ITEM.get(step, "not_run" if run.get("finished") else "running")
            out.append(
                {"system": call.system_key, "operation": call.operation_id, "status": item_status}
            )
    return out
