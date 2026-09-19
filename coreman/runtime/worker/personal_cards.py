"""Human-only Feishu cards (consent, schedule confirmation), bound to a verified private chat."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core import personal_schedules as schedules
from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, InboundEvent, InteractionState, Task, User
from coreman.core.errors import ApiError
from coreman.core.feishu_personal import policy, service
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

OPTIONS = (
    (
        "all",
        "全部权限（含发送消息）",
        "消息、日程、邮件、任务、云文档、审批等应用已开通的个人数据，可读取、创建、修改和删除；"
        "可按你的明确要求以你的身份发消息和邮件。",
    ),
    (
        "all_except_send",
        "全部权限（不含发送消息）",
        "消息、日程、邮件、任务、云文档、审批等应用已开通的个人数据，可读取、创建、修改和删除；"
        "不会以你的身份发消息或邮件。",
    ),
    ("messages_readonly", "仅读取消息", "搜索和读取你有权限访问的私聊、群聊消息；不发送消息。"),
)


def selection_card(task_id: int, *, remote_revoked: bool = True) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": "选择本次允许 AI 员工使用的范围，点击后获取授权链接。"}
    ]
    for index, (level, title, description) in enumerate(OPTIONS, 1):
        elements.extend(
            [
                {"tag": "markdown", "content": f"**{index}. {title}**\n{description}"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"选择{title}"},
                    "type": "primary" if index == 1 else "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "task_id": f"personal:{task_id}",
                                "level": level,
                                "event_key": "personal_authorize",
                            },
                        }
                    ],
                },
            ]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": (
                "仅限本人私聊使用 · 选择有效期 10 分钟\n实际可执行的操作以系统已接入的工具为准。"
                + "\n"
                + service.RETENTION_NOTICE
            ),
        }
    )
    if not remote_revoked:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "旧授权在 CoreMan 中已停止访问，飞书端凭证撤销尚未确认。"
                    "本次仍按新选择限制访问。"
                ),
            }
        )
    return {
        "schema": "2.0",
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "连接飞书"},
        },
        "body": {"elements": elements},
    }


async def card_owner_scope(
    session: AsyncSession, original: Task, inbound: InboundEvent
) -> policy.Scope | None:
    """The clicker must be the verified private-chat speaker of `original`, in that chat.

    Callers must separately check that the card was sent by us to this chat and that
    `original` is the kind of task their card belongs to.
    """
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
    )
    if not speaker.known or speaker.user_id is None:
        return None
    scope = await policy.verified_origin_scope(session, original, str(speaker.user_id))
    raw = inbound.payload.get("raw") or {}
    header, event = raw.get("header") or {}, raw.get("event") or {}
    operator, context = event.get("operator") or {}, event.get("context") or {}
    if (
        header.get("event_type") != "card.action.trigger"
        or header.get("app_id") != scope.app_id
        or (header.get("tenant_key") or operator.get("tenant_key")) != scope.tenant_key
        or inbound.chat_id != scope.chat_id
        or context.get("open_chat_id") != scope.chat_id
        or context.get("open_message_id") != inbound.reply_context.get("message_id")
        or operator.get("user_id") != scope.platform_user_id
        or operator.get("open_id") != scope.open_id
        or inbound.sender_open_id != scope.open_id
    ):
        return None
    return scope


async def handle_selection(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """Called only after CardActionHandler proves this card was sent to this chat."""
    level = action.get("level")
    if level not in {option[0] for option in OPTIONS}:
        return "ignored"
    try:
        original = await session.get(Task, int(str(action["task_id"]).split(":")[1]))
        if (
            original is None
            or original.bot_id != bot.id
            or original.kind != "chat"
            or original.status != "succeeded"
            or original.cancel_requested_at
            or not (original.result or {}).get("personal_authorization_flow")
            or original.payload.get("collaboration_id")
            or original.payload.get("collaboration_phase")
            or ctx.task.kind != "card_action"
            or ctx.task.status not in tasks.ACTIVE
            or ctx.task.cancel_requested_at
            or inbound.platform != "feishu"
            or inbound.kind != "card_action"
            or inbound.bot_id != bot.id
        ):
            return "ignored"
        scope = await card_owner_scope(session, original, inbound)
        if scope is None:
            return "ignored"
    except (ValueError, TypeError, KeyError, AttributeError):
        return "ignored"
    choice = {"all": "1", "all_except_send": "2", "messages_readonly": "3"}[str(level)]
    title = next(option[1] for option in OPTIONS if option[0] == level)
    try:
        result = await service.choose_authorization(
            session,
            ctx.cipher,
            scope,
            choice,
            selection_task_id=original.id,
        )
        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"**已选择：{title}**\n点击下方按钮完成飞书授权，然后回到私聊回复“已授权”。"
                ),
            },
            {
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "前往飞书授权"},
                "behaviors": [{"type": "open_url", "default_url": result["authorization_url"]}],
            },
            {"tag": "markdown", "content": "历史授权不会扩大本次选择的范围。"},
        ]
        outcome = "personal_authorization_pending"
    except (service.PersonalError, ValueError) as exc:
        if getattr(exc, "code", "") == "selection_required":
            # Do not overwrite a newer successful link on a double-click or replay.
            return "expired"
        ctx.log.warning(
            "personal_authorization_unavailable",
            level=level,
            code=getattr(exc, "code", type(exc).__name__),
            upstream_code=getattr(exc, "upstream_code", None),
        )
        text = "暂时无法生成授权链接，请稍后重新发送“连接飞书”。"
        if getattr(exc, "code", "") == "app_scope_discovery_permission_missing":
            text = (
                "应用缺少权限查询能力，请管理员开通 application:application:self_manage "
                "后重试，或重新连接并选择仅读取消息。"
            )
        elif getattr(exc, "code", "") == "app_send_permission_missing":
            text = (
                "应用尚未开通以本人身份发送消息的权限，请管理员开通 "
                "im:message 和 im:message.send_as_user 后重试，或重新连接并选择其他范围。"
            )
        elements = [{"tag": "markdown", "content": text}]
        outcome = "personal_authorization_unavailable"
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
                "header": {"title": {"tag": "plain_text", "content": "飞书授权"}},
                "body": {"elements": elements},
            }
        },
    )
    return outcome


async def _schedule_card(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
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
                "header": {"title": {"tag": "plain_text", "content": "定时任务"}},
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }
        },
    )


async def handle_schedule(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """本人确认或取消模型拟好的定时任务。只有卡片发给的那个私聊里的本人点了才算数。"""
    verb, _, state_id = str(action.get("level") or "").partition(":")
    if verb not in ("confirm", "cancel"):
        return "ignored"
    try:
        original = await session.get(Task, int(str(action["task_id"]).split(":")[1]))
        if (
            original is None
            or original.bot_id != bot.id
            or original.kind != "chat"
            or original.payload.get("collaboration_id")
            or original.payload.get("collaboration_phase")
            or ctx.task.kind != "card_action"
            or ctx.task.status not in tasks.ACTIVE
            or ctx.task.cancel_requested_at
            or inbound.platform != "feishu"
            or inbound.kind != "card_action"
            or inbound.bot_id != bot.id
        ):
            return "ignored"
        scope = await card_owner_scope(session, original, inbound)
        if scope is None:
            return "ignored"
        state = await session.scalar(
            select(InteractionState)
            .where(
                InteractionState.id == uuid.UUID(state_id),
                InteractionState.kind == schedules.KIND,
                InteractionState.bot_id == bot.id,
            )
            .with_for_update()
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return "ignored"
    draft = state.state if state else {}
    if (
        state is None
        or draft.get("user_id") != str(scope.user_id)
        or draft.get("chat_id") != scope.chat_id
        or draft.get("origin_task_id") != original.id
    ):
        return "ignored"
    now = utcnow()
    if state.status != "open" or (state.expires_at is not None and state.expires_at <= now):
        # 重复点击或回放：不覆盖已经给出的结果。
        if state.status in ("submitted", "cancelled"):
            return "expired"
        state.status = "expired"
        await _schedule_card(
            session, bot, inbound, "这张确认卡片已过期，没有创建定时任务。需要的话请重新告诉我。"
        )
        return "expired"
    name = str(draft.get("name") or "")
    if verb == "cancel":
        state.status = "cancelled"
        await _schedule_card(session, bot, inbound, f"已取消，没有创建「{name}」。")
        return "personal_schedule_cancelled"
    actor = await session.get(User, scope.user_id, with_for_update=True, populate_existing=True)
    try:
        if actor is None:
            raise ApiError(403, 403, "reminder_actor_unavailable")
        job = await schedules.create_confirmed(session, bot, actor, scope.chat_id, draft, now)
    except schedules.ScheduleError as exc:
        state.status = "cancelled"
        await _schedule_card(session, bot, inbound, f"没有创建「{name}」：{exc.message}")
        return "personal_schedule_rejected"
    except ApiError:
        state.status = "cancelled"
        await _schedule_card(
            session, bot, inbound, f"没有创建「{name}」：你当前不能使用这个机器人的定时任务。"
        )
        return "personal_schedule_rejected"
    state.status = "submitted"
    manage = ctx.public_base_url.rstrip("/") + "/self-reminders"
    await _schedule_card(
        session,
        bot,
        inbound,
        f"**已创建定时任务「{job.name}」**\n执行时间："
        + schedules.describe(job.schedule_kind, job.cron_expression, job.run_at)
        + f"\n下次运行：{schedules.shown(job.next_run_at)}"
        + f"\n结果只发到这个私聊。[查看或管理]({manage})",
    )
    return "personal_schedule_created"
