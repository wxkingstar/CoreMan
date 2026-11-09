"""群里确认卡的点击：只有提问人本人点才算数，点确认后按暂存参数以他的身份执行。"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.db.models import Bot, FeishuGroupAction, InboundEvent, InteractionState, Task
from coreman.core.feishu_docs import access, confirm, resume, write_tools
from coreman.core.feishu_docs import write_policy as wp
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.feishu_personal.tools import TOOLS
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.personal_cards import group_card_scope


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """调用方已证明卡片确实是我们发到这个会话的。暂存的参数可能含文档内容，不进日志。"""
    verb, _, raw_id = str(action.get("level") or "").partition(":")
    if verb not in ("confirm", "cancel"):
        return "ignored"
    try:
        origin = await session.get(Task, int(str(action["task_id"]).split(":")[1]))
        state_id = uuid.UUID(raw_id)
    except (KeyError, ValueError, IndexError):
        return "ignored"
    if (
        origin is None
        or origin.bot_id != bot.id
        or origin.payload.get("collaboration_id")
        or origin.payload.get("collaboration_phase")
        or ctx.task.kind != "card_action"
        or ctx.task.status not in tasks.ACTIVE
        or ctx.task.cancel_requested_at
        or inbound.platform != "feishu"
        or inbound.kind != "card_action"
        or inbound.bot_id != bot.id
    ):
        return "ignored"
    who = await group_card_scope(session, origin, inbound)
    if who is None:
        return "ignored"
    state = await session.scalar(
        select(InteractionState)
        .where(
            InteractionState.id == state_id,
            InteractionState.kind == confirm.KIND,
            InteractionState.bot_id == bot.id,
        )
        .with_for_update()
    )
    draft = state.state if state else {}
    if (
        state is None
        or draft.get("user_id") != str(who.user_id)
        or draft.get("chat_id") != who.chat_id
        or draft.get("origin_task_id") != origin.id
    ):
        return "ignored"
    if state.status != "open":
        return "expired"
    if state.expires_at is not None and state.expires_at <= utcnow():
        state.status = "expired"
        await _update(session, bot, inbound, "这张确认卡片已过期，没有执行。")
        return "expired"
    event = await session.get(InboundEvent, origin.inbound_event_id)
    assert event is not None  # group_card_scope 已核验过来源消息
    docs_scope = DocsScope(origin, bot, event, draft["chat_id"], who.user_id)
    name, digest, target = str(draft["tool"]), str(draft["digest"]), draft.get("target")
    if verb == "cancel":
        state.status = "cancelled"
        write_tools.record(session, docs_scope, name, "confirm", target, "cancelled", digest)
        await _update(session, bot, inbound, "已取消，没有执行。")
        await _resume(session, origin, state, "用户取消了这次操作，不要重试。")
        return "feishu_group_write_cancelled"
    done = await session.scalar(
        select(FeishuGroupAction.id)
        .where(
            FeishuGroupAction.task_id == origin.id,
            FeishuGroupAction.tool == name,
            FeishuGroupAction.args_hash == digest,
            FeishuGroupAction.status == "succeeded",
        )
        .limit(1)
    )
    if done is not None:
        state.status = "submitted"
        await _update(session, bot, inbound, "这项操作之前已经执行过，没有重复执行。")
        await _resume(session, origin, state, "这项操作之前已成功执行，不要重试，继续后面的步骤。")
        return "feishu_group_write_done"
    item = TOOLS.get(name) if name in wp.GROUP_TOOLS else None
    args: Any = None
    if item is not None:
        try:
            args = item.args.model_validate(draft["arguments"])
        except ValidationError:
            item = None
    if item is None or (name == "feishu_edit_document" and args.command == "overwrite"):
        state.status = "cancelled"
        write_tools.record(session, docs_scope, name, "confirm", target, "failed", digest)
        await _update(session, bot, inbound, "这项操作不允许在群里执行，没有执行。")
        return "feishu_group_write_refused"
    requester = await access.requester(session, docs_scope)
    if requester is None or not requester.reaches(item.endpoint):
        state.status = "cancelled"
        await _update(session, bot, inbound, "你的飞书授权已失效，没有执行。请重新告诉我要做的事。")
        return "feishu_group_write_unauthorized"
    # 先落库再调飞书：调用途中崩溃、任务重试时也不会再执行一次，飞书的长调用也不占着行锁。
    state.status = "submitted"
    await session.commit()
    try:
        await item.run(args, Call(session, ctx.cipher, requester.scope))
    except PersonalError as exc:
        write_tools.record(
            session, docs_scope, name, "confirm", target, "failed", digest, exc.upstream_code
        )
        await _update(session, bot, inbound, "执行失败，飞书返回了错误。")
        await _resume(session, origin, state, "确认后执行失败：" + str(exc.payload()))
        return "feishu_group_write_failed"
    write_tools.record(session, docs_scope, name, "confirm", target, "succeeded", digest)
    await _update(session, bot, inbound, "已确认并执行。")
    await _resume(session, origin, state, "用户已确认，操作已成功执行，继续后面的步骤。")
    return "feishu_group_write_done"


async def _resume(session: AsyncSession, origin: Task, state: InteractionState, note: str) -> None:
    await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=origin.bot_id,
            kind="chat",
            user_id=origin.user_id,
            session_key=origin.session_key,
            inbound_event_id=origin.inbound_event_id,
            dedupe_key=f"feishu-group-write:{state.id}:resume",
            payload={
                **{k: v for k, v in origin.payload.items() if k != resume.PAYLOAD_KEY},
                confirm.RESULT_KEY: note,
                "serialize_session": True,
            },
        ),
    )


async def _update(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={
            "card": {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "飞书操作"}},
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }
        },
    )
