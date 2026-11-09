"""用到飞书能力时才授权：判断够不够、发卡片、记下这一轮，授权完成后由 resume 续接。"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuAuthWait, FeishuPersonalGrant, InboundEvent, Task
from coreman.core.db.models.feishu_inflow import AUTH_WAIT_LIVE
from coreman.core.feishu_personal import endpoints, permissions, policy, service
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.timeutils import utcnow

UPGRADE_EVENT = "personal_upgrade"
WAIT_TTL = timedelta(minutes=30)
TARGET_LEVEL = {
    "general": "all_except_send",
    "read_messages": "all_except_send",
    "send_messages": "all",
}
_RANK = {"general": 0, "read_messages": 0, "send_messages": 1}
_APP_UNAVAILABLE = (
    "app_send_permission_missing",
    "app_message_permission_missing",
    "authorization_unavailable",
)
_ENOUGH = {
    "general": ("all", "all_except_send", "no_messages"),
    "read_messages": ("all", "all_except_send", "messages_readonly"),
    "send_messages": ("all",),
}


def satisfied(row: Any, need: str) -> bool:
    return (
        row is not None
        and row.status == "connected"
        and bool(row.token_enc)
        and row.authorization_level in _ENOUGH[need]
    )


def _reaches(row: FeishuPersonalGrant, keys: Iterable[str]) -> bool:
    granted = frozenset(row.requested_scopes or []) & frozenset(row.scopes or [])
    return all(
        endpoints.denied(endpoints.ENDPOINTS[key], row.authorization_level, granted) is None
        for key in keys
    )


def link_card(url: str, purpose: str) -> dict[str, Any]:
    head = f"要继续「{purpose}」，需要你授权一次飞书。" if purpose else "需要你授权一次飞书。"
    return _card(
        head + "\n范围：全部权限（不代发消息）。点完后会自动继续，不用回到这里回复。",
        {
            "tag": "button",
            "type": "primary",
            "text": {"tag": "plain_text", "content": "一键授权"},
            "behaviors": [{"type": "open_url", "default_url": url}],
        },
    )


def upgrade_card(task_id: int, wait_id: Any, purpose: str) -> dict[str, Any]:
    head = f"要继续「{purpose}」，当前飞书授权不够。" if purpose else "当前飞书授权不够。"
    return {
        **_card(
            head + "\n点击后原授权会被替换，完成后自动继续。",
            {
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "升级授权"},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "task_id": f"personal:{task_id}",
                            "event_key": UPGRADE_EVENT,
                            "level": str(wait_id),
                        },
                    }
                ],
            },
        ),
        "task_id": f"personal:{task_id}",
    }


def _card(text: str, button: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": "飞书授权"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
                button,
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>{service.RETENTION_NOTICE}</font>",
                },
            ]
        },
    }


async def _live(session: AsyncSession, scope: policy.Scope) -> list[FeishuAuthWait]:
    return list(
        await session.scalars(
            select(FeishuAuthWait)
            .where(
                FeishuAuthWait.bot_id == scope.bot.id,
                FeishuAuthWait.user_id == scope.user_id,
                FeishuAuthWait.status.in_(AUTH_WAIT_LIVE),
                FeishuAuthWait.expires_at > utcnow(),
            )
            .with_for_update()
        )
    )


async def _send(session: AsyncSession, scope: policy.Scope, card: dict[str, Any], key: str) -> None:
    """授权卡片只发给提问人本人：群里触发时走私信，绝不把链接发进群。"""
    event = scope.event
    payload: dict[str, Any] = {"card": card}
    target = {"chat_id": scope.chat_id}
    if event is not None and event.chat_type == "group":
        target = {"user_id": scope.platform_user_id}
        payload["_dm_fallback"] = {
            "chat_id": event.chat_id,
            "user_id": scope.platform_user_id,
            "text": (
                "我没法私信你发授权卡片，可能是你不在这个机器人的可用范围内，请联系机器人管理员。"
            ),
        }
    await outbox.add(
        session,
        bot_id=scope.bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target=target,
        payload=payload,
    )


async def request(
    session: AsyncSession,
    cipher: Cipher,
    scope: policy.Scope,
    *,
    need: str = "general",
    purpose: str = "",
    endpoints_needed: Iterable[str] = (),
) -> dict[str, Any]:
    keys = tuple(endpoints_needed)
    await service.ensure_grant_row(session, cipher, scope)
    row = await session.get(
        FeishuPersonalGrant, (scope.bot.id, scope.user_id), populate_existing=True
    )
    assert row is not None
    connected = row.status == "connected" and bool(row.token_enc)
    if satisfied(row, need) and _reaches(row, keys):
        return {"status": "already_connected"}
    if connected and keys:
        _, secret = policy.app_credentials(cipher, scope.bot)
        available = set(await permissions.app_user_scopes(row.app_id, secret, http=service._http))
        for key in keys:
            endpoint = endpoints.ENDPOINTS[key]
            if not available.intersection(endpoint.scopes) or not endpoint.also <= available:
                return {"status": "app_permission_missing"}
    live = await _live(session, scope)
    now = utcnow()
    same = next((w for w in live if w.chat_id == scope.chat_id), None)
    offered_elsewhere = any(w.status == "offered" for w in live)
    link_alive = (
        row.status == "pending"
        and row.pending_expires_at is not None
        and row.pending_expires_at > now
    )
    sent = {
        "status": "authorization_sent",
        "hint": "授权卡片已发到与你的私聊，点完后会自动继续。简短说明后结束本轮。",
    }
    if not (offered_elsewhere or link_alive) and not connected:
        # 没有可用的卡片：先发起授权，失败就不留等待记录。
        try:
            begun = await service.begin_in_flow(session, cipher, scope, TARGET_LEVEL[need])
        except PersonalError as exc:
            if exc.code not in _APP_UNAVAILABLE:
                raise
            return {
                "status": "app_permission_missing",
                "hint": "这个机器人的飞书应用权限不足以发起授权，请联系机器人管理员。",
            }
    else:
        begun = None
    wait_status = "offered" if connected and not link_alive else "waiting"
    if same is not None:
        same.origin_task_id, same.expires_at = scope.task.id, now + WAIT_TTL
        same.status = wait_status
        if _RANK[need] > _RANK[same.need]:
            same.need = need
        wait = same
    else:
        wait = FeishuAuthWait(
            bot_id=scope.bot.id,
            user_id=scope.user_id,
            chat_id=scope.chat_id,
            origin_task_id=scope.task.id,
            need=need,
            status=wait_status,
            expires_at=now + WAIT_TTL,
        )
        session.add(wait)
        await session.flush()
    if offered_elsewhere or link_alive:
        # 同一个人同一时刻只留一张有效卡片，这个聊天只登记等待。
        return sent
    if begun is None:
        # 已连接但不够：旧令牌留着，用户点「升级授权」后才作废。
        card = upgrade_card(scope.task.id, wait.id, purpose)
        await _send(session, scope, card, f"feishu-auth:{wait.id}:upgrade")
        return {
            "status": "upgrade_offered",
            "hint": "升级授权卡片已私信发给提问人，点完后会自动继续。简短说明后结束本轮。",
        }
    card = link_card(begun["authorization_url"], purpose)
    await _send(session, scope, card, f"feishu-auth:{wait.id}:link:{row.context_epoch}")
    return sent


def _click_tenant(inbound: Any) -> str | None:
    raw = inbound.payload.get("raw") or {}
    header, event = raw.get("header") or {}, raw.get("event") or {}
    operator = event.get("operator") or {}
    return header.get("tenant_key") or operator.get("tenant_key")


async def handle_upgrade(
    session: AsyncSession, ctx: Any, bot: Any, inbound: Any, action: dict[str, Any]
) -> str:
    try:
        wait = await session.get(
            FeishuAuthWait, uuid.UUID(str(action.get("level"))), with_for_update=True
        )
    except ValueError:
        return "ignored"
    if wait is None or wait.bot_id != bot.id:
        return "ignored"
    row = await session.get(FeishuPersonalGrant, (wait.bot_id, wait.user_id))
    if (
        row is None
        or inbound.sender_platform_user_id != row.platform_user_id
        or inbound.sender_open_id != row.open_id
        or _click_tenant(inbound) != row.tenant_key
    ):
        return "ignored"
    origin = await session.get(Task, wait.origin_task_id) if wait.origin_task_id else None
    event = await session.get(InboundEvent, origin.inbound_event_id) if origin else None
    if wait.status in AUTH_WAIT_LIVE and wait.expires_at <= utcnow():
        wait.status = "expired"
    if wait.status not in AUTH_WAIT_LIVE or origin is None:
        await _update(session, bot, inbound, "这张卡片已过期，再告诉我一次要做的事就行。")
        return "expired"
    scope = policy.Scope(
        origin,
        bot,
        wait.user_id,
        row.tenant_key,
        row.app_id,
        row.open_id,
        row.platform_user_id,
        wait.chat_id,
        event,
    )
    try:
        begun = await service.begin_in_flow(session, ctx.cipher, scope, TARGET_LEVEL[wait.need])
    except (PersonalError, ValueError):
        await _update(session, bot, inbound, "暂时无法生成授权链接，请稍后再试。")
        return "personal_authorization_unavailable"
    wait.status = "waiting"
    await _update(
        session,
        bot,
        inbound,
        "点下面的按钮完成授权，完成后会自动继续。",
        begun["authorization_url"],
    )
    return "personal_authorization_pending"


async def _update(
    session: AsyncSession, bot: Any, inbound: Any, text: str, url: str | None = None
) -> None:
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text}]
    if url:
        elements.append(
            {
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "前往飞书授权"},
                "behaviors": [{"type": "open_url", "default_url": url}],
            }
        )
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
