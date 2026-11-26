"""业务系统发起的任务里的写操作：确认卡发到哪里、谁能点、查询时的写操作状态。

私聊模式：API 这一轮没有人在飞书里打字，但发起人就是员工本人，结果本来就发在他和 AI 员工的
私聊里：写操作照常暂存，本轮结束时把确认卡发到这个私聊，只有他本人在这里点才算。见
docs/superpowers/specs/2026-11-24-api-tasks-system-clients.md 第 6.4 节。

群承接模式：暂存人是服务账号，确认卡发到群里，群成员中在使用白名单里的人都能点，按点卡人的
身份执行。见 docs/superpowers/specs/2026-11-25-api-client-group-delivery.md 第 6 节。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    ApiTask,
    Bot,
    InboundEvent,
    InteractionState,
    SystemCall,
    Task,
    User,
)
from coreman.core.timeutils import utcnow


def session_key_of(row: ApiTask) -> str:
    """API 任务各轮所在的会话：私聊模式是它自己的独立会话，群承接模式是群的主会话。"""
    from coreman.core.api_tasks import groups
    from coreman.core.api_tasks.service import client_session_key

    return row.chat_id if groups.is_group_row(row) else client_session_key(row.id)


async def client_task(session: AsyncSession, task: Task) -> ApiTask | None:
    """`task` 是业务系统发起的那一轮（或它的续接）时，返回对应的 API 任务。"""
    from coreman.core.api_tasks import groups

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
        or groups.is_group_row(row) != groups.is_group_payload(payload)
        or task.session_key != session_key_of(row)
    ):
        return None
    return row


async def confirm_origin(
    session: AsyncSession, task: Task, user_id: uuid.UUID
) -> tuple[str, str] | None:
    """确认卡发往哪里：私聊模式是员工私聊 (chat_id, "single")，群承接模式是群 (chat_id,
    "group")。暂存人不是这个任务的执行身份时返回 None。"""
    from coreman.core.api_tasks import groups
    from coreman.core.api_tasks.service import allowed_to_use

    row = await client_task(session, task)
    if row is None or row.user_id != user_id:
        return None
    bot = await session.get(Bot, task.bot_id)
    if bot is None or bot.platform != "feishu" or not bot.enabled:
        return None
    if groups.is_group_row(row):
        # 执行身份是配置死的服务账号，不看使用白名单；能点卡的人另按群成员和白名单核对。
        user = await session.get(User, user_id)
        if not groups.service_usable(user) or await groups.route_of(session, row) is None:
            return None
        return row.chat_id, "group"
    if not await allowed_to_use(session, bot, user_id):
        return None
    return row.chat_id, "single"


def _card_event_ok(inbound: InboundEvent, chat_id: str) -> bool:
    """卡片回调确实是这个会话里这张卡上的点击，点击人就是回调里的操作人。"""
    raw = inbound.payload.get("raw") or {}
    header, event = raw.get("header") or {}, raw.get("event") or {}
    operator, context = event.get("operator") or {}, event.get("context") or {}
    return (
        inbound.platform == "feishu"
        and inbound.kind == "card_action"
        and header.get("event_type") == "card.action.trigger"
        and inbound.chat_id == chat_id
        and context.get("open_chat_id") == chat_id
        and context.get("open_message_id") == inbound.reply_context.get("message_id")
        and bool(inbound.sender_open_id)
        and operator.get("open_id") == inbound.sender_open_id
        and operator.get("user_id") == inbound.sender_platform_user_id
    )


async def card_clicker(
    session: AsyncSession, origin: Task, inbound: InboundEvent, cipher: Cipher
) -> tuple[uuid.UUID, str] | None:
    """(user_id, chat_id)：私聊模式点卡的必须是发起人本人，在他和本 AI 员工的私聊里点；
    群承接模式是这个群里、在 AI 员工使用白名单里（空 = 不限）的任何成员。

    调用方另行核验卡片确实是我们发到这个会话的。飞书的卡片回调不带会话类型，私聊只认真实
    私聊消息留下的记录。
    """
    from coreman.core.api_tasks import groups
    from coreman.core.chat.identity import resolve_speaker
    from coreman.core.chat.reachability import private_chat

    row = await client_task(session, origin)
    if row is None:
        return None
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
    )
    if groups.is_group_row(row):
        return await _group_clicker(session, origin, row, inbound, speaker.user_id, cipher)
    if not speaker.known or speaker.user_id != row.user_id:
        return None
    if not _card_event_ok(inbound, row.chat_id):
        return None
    if not await private_chat(session, origin.bot_id, row.user_id, row.chat_id):
        return None
    return row.user_id, row.chat_id


async def _group_clicker(
    session: AsyncSession,
    origin: Task,
    row: ApiTask,
    inbound: InboundEvent,
    user_id: uuid.UUID | None,
    cipher: Cipher,
) -> tuple[uuid.UUID, str] | None:
    """群承接模式的点卡人：认得出的成员、在使用白名单里、飞书确认还在这个群里。"""
    from coreman.core.api_tasks.service import allowed_to_use
    from coreman.core.chat.human_collaboration import in_group

    if user_id is None or not _card_event_ok(inbound, row.chat_id):
        return None
    clicker = await session.get(User, user_id)
    if clicker is None or clicker.status != "active" or clicker.source == "service":
        return None
    bot = await session.get(Bot, origin.bot_id)
    if bot is None or bot.platform != "feishu" or not bot.enabled:
        return None
    if not await allowed_to_use(session, bot, user_id):
        return None
    if not await in_group(bot, row.chat_id, inbound.sender_platform_user_id or "", cipher):
        return None
    return user_id, row.chat_id


async def group_origin(session: AsyncSession, origin: Task) -> bool:
    """`origin` 是群承接模式的 API 任务（或它的续接）：确认卡谁都能点，按点卡人执行。"""
    from coreman.core.api_tasks import groups

    if not groups.is_group_payload(origin.payload):
        return False
    row = await client_task(session, origin)
    return row is not None and groups.is_group_row(row)


# 单个写操作在 API 查询里的状态。
_ITEM = {"ok": "succeeded", "failed": "failed", "unknown": "unknown", "sending": "unknown"}
# 不经确认卡的写操作（业务系统调用记录的结果）在 API 查询里的状态：名单外被拒，或静默执行。
_DIRECT = {
    "not_allowed": "not_allowed",
    "silent_ok": "succeeded",
    "silent_http_error": "failed",
    # 连接中断时业务系统可能已经处理了：和确认后执行一样报「不确定」，请调用方核对。
    "silent_unreachable": "unknown",
}


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
    from coreman.core.api_tasks import groups
    from coreman.core.systems_catalog import batch, confirm

    turns = select(Task.id).where(
        Task.bot_id == row.bot_id, Task.session_key == session_key_of(row)
    )
    if groups.is_group_row(row):
        # 群主会话里还有群里的人自己的轮次：只看载荷指向这个任务的（群承接不用于个人助理，
        # 载荷是明文）。
        turns = turns.where(Task.payload["api_task_id"].astext == str(row.id))
    task_ids = list(await session.scalars(turns.order_by(Task.id)))
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
    # 个人助理的批次状态是密文：显式还原后再读（普通 AI 员工原样不动）。
    await sealed.open_rows(session, states)
    calls = list(
        await session.scalars(
            select(SystemCall)
            .where(
                SystemCall.task_id.in_(task_ids),
                SystemCall.outcome.in_(("proposed", *_DIRECT)),
            )
            .order_by(SystemCall.id)
        )
    )
    proposed = [c for c in calls if c.outcome == "proposed"]
    now = utcnow()
    # 按发生先后排：确认卡按暂存时间，直接的（名单外被拒、静默执行）按调用时间。
    timed: list[tuple[Any, list[dict[str, Any]]]] = [
        (
            call.called_at,
            [
                {
                    "system": call.system_key,
                    "operation": call.operation_id,
                    "status": _DIRECT[call.outcome],
                    **({"silent": True} if call.outcome.startswith("silent_") else {}),
                }
            ],
        )
        for call in calls
        if call.outcome in _DIRECT
    ]
    for state in states:
        out: list[dict[str, Any]] = []
        timed.append((state.created_at, out))
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
        staged_calls = [c for c in proposed if str(c.task_id) == state.scope_key]
        run = staged.get(confirm.RUN) or {}
        progress = [str(s) for s in run.get("progress") or []]
        for index, call in enumerate(staged_calls):
            item_status = status
            if status == "confirmed":
                step = progress[index] if index < len(progress) else "pending"
                item_status = _ITEM.get(step, "not_run" if run.get("finished") else "running")
            out.append(
                {"system": call.system_key, "operation": call.operation_id, "status": item_status}
            )
    timed.sort(key=lambda pair: pair[0])
    return [entry for _, entries in timed for entry in entries]
