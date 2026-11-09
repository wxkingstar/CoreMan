"""群里需要确认的写入：平台暂存参数，提问人点确认后原样执行，不经过模型。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.db.models import Bot, FeishuGroupAction, InboundEvent, InteractionState, Task
from coreman.core.feishu_docs import write_policy as wp
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.timeutils import utcnow

if TYPE_CHECKING:
    from coreman.runtime.worker.context import TaskContext

CARD_EVENT = "feishu_group_write"
KIND = "feishu_group_write"
TTL = timedelta(minutes=10)
RESULT_KEY = "feishu_group_write_result"
MAX_VALUE = 500
TITLES = {
    "feishu_calendar_update_event": "修改日程",
    "feishu_calendar_add_attendees": "邀请参会人",
    "feishu_calendar_delete_event": "删除日程",
    "feishu_calendar_reply": "回复日程邀请",
    "feishu_calendar_create_event": "创建日程并邀请他人",
    "feishu_task_create": "创建任务并指派他人",
    "feishu_task_add_subtask": "创建子任务并指派他人",
    "feishu_task_update": "修改任务",
    "feishu_task_delete": "删除任务",
    "feishu_share_document": "修改文档权限",
    "feishu_move_file": "移动文件",
    "feishu_base_delete_record": "删除多维表记录",
}
PENDING = {
    "status": "pending_confirmation",
    "hint": "已发确认卡片，提问人点确认后才执行。说一句请他确认，然后结束本轮，不要说已经完成。",
}


def _lines(arguments: dict[str, Any]) -> str:
    """卡片内容来自实际参数，不用模型写的描述。JSON 转义换行，一个值伪造不出别的行。"""
    rows = []
    for key, value in arguments.items():
        text = json.dumps(value, ensure_ascii=False, default=str)
        rows.append(f"- {key}：{text[:MAX_VALUE]}{'…' if len(text) > MAX_VALUE else ''}")
    return "\n".join(rows)


def card(
    origin_task_id: int, state_id: uuid.UUID, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    def button(label: str, verb: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": f"personal:{origin_task_id}",
                        "event_key": CARD_EVENT,
                        "level": f"{verb}:{state_id}",
                    },
                }
            ],
        }

    title = TITLES.get(name, "修改飞书内容")
    return {
        "schema": "2.0",
        "task_id": f"personal:{origin_task_id}",
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"确认：{title}"},
        },
        "body": {
            "elements": [
                # 参数可能受文档内容影响，用纯文本展示，不让其中的标记（@所有人、链接）生效。
                {"tag": "div", "text": {"tag": "plain_text", "content": _lines(arguments)}},
                button("确认执行", "confirm", "primary"),
                button("取消", "cancel", "default"),
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>只有提问人能操作，将以提问人身份执行，"
                        "10 分钟内有效。</font>"
                    ),
                },
            ]
        },
    }


async def propose(
    session: AsyncSession,
    scope: DocsScope,
    name: str,
    arguments: dict[str, Any],
    target: str | None,
    digest: str,
) -> dict[str, Any]:
    from coreman.core.feishu_docs import write_tools

    # 按任务串行，同参数并发提议时只出一张卡。
    await session.scalar(select(Task.id).where(Task.id == scope.task.id).with_for_update())
    prefix = f"{scope.task.id}:{digest}:"
    now = utcnow()
    existing = await session.scalar(
        select(InteractionState.id)
        .where(
            InteractionState.kind == KIND,
            InteractionState.bot_id == scope.bot.id,
            InteractionState.scope_key.startswith(prefix, autoescape=True),
            InteractionState.status == "open",
            InteractionState.expires_at > now,
        )
        .limit(1)
    )
    if existing is not None:
        return dict(PENDING)
    state = InteractionState(
        bot_id=scope.bot.id,
        kind=KIND,
        # 带 uuid：上一张卡结束后同参数还能再提，不撞 (kind, scope_key) 唯一约束。
        scope_key=f"{prefix}{uuid.uuid4()}",
        state={
            "tool": name,
            "arguments": arguments,
            "target": target,
            "digest": digest,
            "user_id": str(scope.speaker_user_id),
            "chat_id": scope.chat_id,
            "origin_task_id": scope.task.id,
        },
        status="open",
        expires_at=now + TTL,
    )
    session.add(state)
    await session.flush()
    await outbox.add(
        session,
        bot_id=scope.bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"feishu-group-write:{state.id}",
        target={"chat_id": scope.chat_id},
        payload={"card": card(scope.task.id, state.id, name, arguments)},
    )
    write_tools.record(session, scope, name, "confirm", target, "pending_confirmation", digest)
    return dict(PENDING)


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """调用方已证明卡片确实是我们发到这个会话的。暂存的参数可能含文档内容，不进日志。"""
    from coreman.core.feishu_docs import access, write_tools
    from coreman.core.feishu_personal.service import PersonalError
    from coreman.core.feishu_personal.toolbase import Call
    from coreman.core.feishu_personal.tools import TOOLS
    from coreman.runtime.worker.personal_cards import group_card_scope

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
            InteractionState.kind == KIND,
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
            payload={**origin.payload, RESULT_KEY: note, "serialize_session": True},
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
