"""Human-only Feishu consent cards, bound to the original verified private message."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, InboundEvent, Task
from coreman.core.feishu_personal import policy, service
from coreman.runtime.worker.context import TaskContext

OPTIONS = (
    (
        "all",
        "全部权限（含发送消息）",
        "读取、修改、删除和管理应用已开通的个人数据；可按你的明确要求发送消息。",
    ),
    (
        "all_except_send",
        "全部权限（不含发送消息）",
        "读取、修改、删除和管理应用已开通的个人数据；禁止发送消息。",
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
        speaker = await resolve_speaker(
            session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
        )
        if not speaker.known or speaker.user_id is None:
            return "ignored"
        scope = await policy.verified_origin_scope(session, original, str(speaker.user_id))
        raw = inbound.payload.get("raw") or {}
        header, event = raw.get("header") or {}, raw.get("event") or {}
        operator, context = event.get("operator") or {}, event.get("context") or {}
        if (
            header.get("event_type") != "card.action.trigger"
            or header.get("app_id") != scope.app_id
            or (header.get("tenant_key") or operator.get("tenant_key")) != scope.tenant_key
            or inbound.chat_id != scope.event.chat_id
            or context.get("open_chat_id") != scope.event.chat_id
            or context.get("open_message_id") != inbound.reply_context.get("message_id")
            or operator.get("user_id") != scope.event.sender_platform_user_id
            or operator.get("open_id") != scope.event.sender_open_id
            or inbound.sender_open_id != scope.event.sender_open_id
        ):
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
