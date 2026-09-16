"""Permission-bound, non-AI Feishu probes for administrator route setup."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.permissions import can_edit_bot
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    BotCollaborationPartner,
    BotCollaborationRoute,
    BotMember,
    InboundEvent,
    OutboxItem,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.runtime_nodes.transport import online

PROBE_SECONDS = 300


def credentials(bot: Bot, cipher: Cipher) -> dict[str, str]:
    try:
        value = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("飞书凭证无法读取，请重新保存应用配置") from None
    if bot.platform != "feishu" or not value.get("app_id") or not value.get("app_secret"):
        raise ValueError("请先配置有效的飞书应用凭证")
    return value


def fingerprint(bot: Bot) -> str:
    return hashlib.sha256(bot.credentials_enc.encode()).hexdigest()


async def manageable(session: AsyncSession, user: User, bot: Bot) -> bool:
    members = list(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot.id))
    )
    return user.status == "active" and can_edit_bot(user, bot, members)


async def available(session: AsyncSession, bot: Bot) -> bool:
    if not bot.enabled or bot.platform != "feishu" or not bot.relay_server_id:
        return False
    relay = await session.get(RelayServer, bot.relay_server_id, populate_existing=True)
    if not relay or not relay.is_active or not relay.runtime_node_id:
        return False
    node = await session.get(RuntimeNode, relay.runtime_node_id, populate_existing=True)
    return bool(node and node.is_active and not node.draining and online(node))


async def _groups(client: FeishuClient) -> dict[str, str]:
    groups: dict[str, str] = {}
    cursor = ""
    seen: set[str] = set()
    for _ in range(20):
        body = await client.call(
            "GET", "/open-apis/im/v1/chats", params={"page_size": 100, "page_token": cursor}
        )
        data = body.get("data") or {}
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("飞书群列表返回异常，请检查应用权限后重试")
        for row in items:
            if isinstance(row, dict) and row.get("chat_id") and row.get("name"):
                groups[str(row["chat_id"])] = str(row["name"])
        if data.get("has_more") is False:
            return groups
        cursor = str(data.get("page_token") or "")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("飞书群列表过大或分页异常，请缩小应用群范围后重试")


async def common_groups(source: Bot, target: Bot, cipher: Cipher) -> list[dict[str, str]]:
    left, right = credentials(source, cipher), credentials(target, cipher)
    if left["app_id"] == right["app_id"]:
        raise ValueError("两个AI员工不能使用同一个飞书应用建立协作")
    a, b = (
        FeishuClient(left["app_id"], left["app_secret"]),
        FeishuClient(right["app_id"], right["app_secret"]),
    )
    try:
        first, second = await _groups(a), await _groups(b)
        return [
            {"chat_id": cid, "name": first[cid]} for cid in sorted(first.keys() & second.keys())
        ]
    finally:
        await a.aclose()
        await b.aclose()


async def _open_id(bot: Bot, cipher: Cipher) -> str:
    config = credentials(bot, cipher)
    client = FeishuClient(config["app_id"], config["app_secret"])
    try:
        body = await client.call("GET", "/open-apis/bot/v3/info/")
        identity = body.get("bot") or (body.get("data") or {}).get("bot") or {}
        value = identity.get("open_id")
        if not isinstance(value, str) or not value:
            raise ValueError("飞书未返回机器人身份，请检查应用发布与机器人能力")
        return value
    finally:
        await client.aclose()


async def begin(
    session: AsyncSession,
    route: BotCollaborationRoute,
    source: Bot,
    target: Bot,
    user: User,
    cipher: Cipher,
    chat_name: str | None = None,
) -> None:
    if (
        source.id == target.id
        or route.source_bot_id != source.id
        or route.target_bot_id != target.id
    ):
        raise ValueError("协作双方配置不匹配")
    if not await manageable(session, user, source) or not await manageable(session, user, target):
        raise ValueError("需要同时管理两个AI员工才能验证协作")
    if not await available(session, source) or not await available(session, target):
        raise ValueError("请先启用两个AI员工并确保运行时在线")
    groups = await common_groups(source, target, cipher)
    chosen = next((g for g in groups if g["chat_id"] == route.chat_id), None)
    if chosen is None:
        raise ValueError("所选群已不在双方共同群中，请重新选择")
    try:
        async with asyncio.timeout(15):
            source_open, target_open = (
                await _open_id(source, cipher),
                await _open_id(target, cipher),
            )
    except (FeishuError, TimeoutError):
        raise ValueError("暂时无法验证协作机器人身份，本轮已停止") from None
    if source_open == target_open:
        raise ValueError("协作双方机器人身份不能相同")
    probe = str(uuid.uuid4())
    meta: dict[str, Any] = {
        "status": "pending",
        "reason": None,
        "probe_id": probe,
        "actor_id": str(user.id),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=PROBE_SECONDS)).isoformat(),
        "chat_name": chosen["name"],
        "source_fingerprint": fingerprint(source),
        "target_fingerprint": fingerprint(target),
        "source_app_id": credentials(source, cipher)["app_id"],
        "target_app_id": credentials(target, cipher)["app_id"],
        "source_open_id": source_open,
        "target_open_id": target_open,
    }
    route.enabled, route.archived = False, False
    route.source_open_id, route.target_open_id = source_open, target_open
    route.tenant_key = route.source_union_id = route.target_union_id = ""
    await session.flush()
    for side, sender, receiver_open in (
        ("source", source, target_open),
        ("target", target, source_open),
    ):
        item = await outbox.add(
            session,
            bot_id=sender.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"collaboration-setup:{route.id}:{probe}:{side}",
            target={"chat_id": route.chat_id},
            payload={
                "markdown": "CoreMan 协作连接测试：验证机器人之间的群消息接收，不会启动AI任务。",
                "_collaboration_setup_id": str(route.id),
                "_setup_probe_id": probe,
                "_setup_side": side,
                "_mention_open_id": receiver_open,
            },
        )
        assert item is not None
        meta[f"{side}_outbox_id"] = item.id
    route.setup = meta
    await session.flush()


async def check_current_group(source: Bot, target: Bot, chat_id: str, cipher: Cipher) -> None:
    """One bounded check per bot using that bot's own tenant token; never list groups."""
    configs = [credentials(bot, cipher) for bot in (source, target)]
    if configs[0]["app_id"] == configs[1]["app_id"]:
        raise ValueError("两个 AI 员工不能使用同一个飞书应用建立协作")

    async def check(bot: Bot, config: dict[str, str]) -> None:
        client = FeishuClient(config["app_id"], config["app_secret"])
        try:
            async with asyncio.timeout(10):
                token = await client.get_token()
                body = await client.call(
                    "GET",
                    f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}/members/is_in_chat",
                    token=token,
                )
            member = (body.get("data") or {}).get("is_in_chat")
            if member is False:
                raise ValueError(f"「{bot.name}」不在当前群，请先将其加入群聊后重新发起任务")
            if member is not True:
                raise FeishuError(-2, "invalid membership response")
        except FeishuError as exc:
            if exc.code == 99991672:
                raise ValueError(
                    f"无法确认「{bot.name}」是否在当前群：应用缺少群成员查询权限，"
                    "请管理员开启 im:chat.members:read 并发布应用，本轮不再重试"
                ) from None
            if exc.code == 232010:
                raise ValueError(
                    f"无法确认「{bot.name}」是否在当前群：应用与群不在同一租户，本轮不再重试"
                ) from None
            raise ValueError(f"暂时无法确认「{bot.name}」是否在当前群，本轮不再重试") from None
        except (TimeoutError, TypeError, AttributeError):
            raise ValueError(f"暂时无法确认「{bot.name}」是否在当前群，本轮不再重试") from None
        finally:
            await client.aclose()

    results = await asyncio.gather(
        *(check(bot, config) for bot, config in zip((source, target), configs, strict=True)),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def begin_runtime(
    session: AsyncSession,
    route: BotCollaborationRoute,
    source: Bot,
    target: Bot,
    actor_id: uuid.UUID,
    partner: BotCollaborationPartner,
    cipher: Cipher,
) -> None:
    """Lazy transport handshake after membership and original-human ACL checks."""
    try:
        async with asyncio.timeout(15):
            source_open, target_open = (
                await _open_id(source, cipher),
                await _open_id(target, cipher),
            )
    except (FeishuError, TimeoutError):
        raise ValueError("暂时无法验证协作机器人身份，本轮已停止") from None
    if source_open == target_open:
        raise ValueError("协作双方机器人身份不能相同")
    probe = str(uuid.uuid4())
    meta: dict[str, Any] = {
        "status": "pending",
        "reason": None,
        "probe_id": probe,
        "actor_id": str(actor_id),
        "partner_id": str(partner.id),
        "runtime_request": True,
        "expires_at": (
            datetime.now(UTC)
            + timedelta(seconds=min(PROBE_SECONDS, max(30, partner.timeout_seconds)))
        ).isoformat(),
        "chat_name": route.chat_id,
        "source_fingerprint": fingerprint(source),
        "target_fingerprint": fingerprint(target),
        "source_app_id": credentials(source, cipher)["app_id"],
        "target_app_id": credentials(target, cipher)["app_id"],
        "source_open_id": source_open,
        "target_open_id": target_open,
    }
    route.enabled, route.archived = True, False
    route.source_open_id, route.target_open_id = source_open, target_open
    route.tenant_key = route.source_union_id = route.target_union_id = ""
    await session.flush()
    for side, sender, receiver_open in (
        ("source", source, target_open),
        ("target", target, source_open),
    ):
        item = await outbox.add(
            session,
            bot_id=sender.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"collaboration-setup:{route.id}:{probe}:{side}",
            target={"chat_id": route.chat_id},
            payload={
                "markdown": "正在建立本次协作连接，核验双方机器人身份，不会启动 AI 任务。",
                "_collaboration_setup_id": str(route.id),
                "_setup_probe_id": probe,
                "_setup_side": side,
                "_mention_open_id": receiver_open,
            },
        )
        assert item is not None
        meta[f"{side}_outbox_id"] = item.id
    route.setup = meta
    await session.flush()


async def _current(
    session: AsyncSession, route: BotCollaborationRoute
) -> tuple[Bot, Bot, User | None]:
    meta = route.setup or {}
    source = await session.get(Bot, route.source_bot_id, populate_existing=True)
    target = await session.get(Bot, route.target_bot_id, populate_existing=True)
    if route.archived or meta.get("cancelled") or meta.get("status") not in {"pending", "ready"}:
        raise ValueError("verification_cancelled")
    if not source or not target or source.id == target.id:
        raise ValueError("employee_unavailable")
    if fingerprint(source) != meta.get("source_fingerprint") or fingerprint(target) != meta.get(
        "target_fingerprint"
    ):
        raise ValueError("credentials_changed")
    try:
        actor = await session.get(User, uuid.UUID(meta.get("actor_id", "")), populate_existing=True)
    except (ValueError, TypeError, AttributeError):
        actor = None
    if meta.get("runtime_request"):
        try:
            partner = await session.get(
                BotCollaborationPartner,
                uuid.UUID(meta.get("partner_id", "")),
                populate_existing=True,
            )
        except (ValueError, TypeError):
            partner = None
        if not partner or partner.source_bot_id != source.id or partner.target_bot_id != target.id:
            raise ValueError("permission_revoked")
        # The current requesting human is checked separately for every ledger operation.
        if not partner.enabled or partner.archived:
            raise ValueError("permission_revoked")
        if meta.get("status") == "pending":
            if not actor or actor.status != "active":
                raise ValueError("permission_revoked")
            for bot in (source, target):
                allowed = set(
                    await session.scalars(
                        select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id)
                    )
                )
                if allowed and actor.id not in allowed:
                    raise ValueError("permission_revoked")
    elif (
        not actor
        or not await manageable(session, actor, source)
        or not await manageable(session, actor, target)
    ):
        raise ValueError("permission_revoked")
    if not await available(session, source) or not await available(session, target):
        raise ValueError("runtime_unavailable")
    return source, target, actor


def _expired(meta: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(meta["expires_at"]) <= datetime.now(UTC)
    except (KeyError, ValueError, TypeError):
        return True


async def guard_probe(
    session: AsyncSession, item: OutboxItem, *, credentials_fingerprint: str | None = None
) -> None:
    """Read-only guard under outbox lock; never acquire a route lock in reverse order."""
    try:
        route = await session.get(
            BotCollaborationRoute,
            uuid.UUID(item.payload["_collaboration_setup_id"]),
            populate_existing=True,
        )
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid_setup_probe") from None
    if not route:
        raise ValueError("verification_cancelled")
    await _current(session, route)
    meta = route.setup or {}
    side = item.payload.get("_setup_side")
    if credentials_fingerprint is not None and credentials_fingerprint != meta.get(
        f"{side}_fingerprint"
    ):
        raise ValueError("credentials_changed")
    if (
        meta.get("status") != "pending"
        or _expired(meta)
        or side not in {"source", "target"}
        or item.payload.get("_setup_probe_id") != meta.get("probe_id")
        or meta.get(f"{side}_outbox_id") != item.id
        or item.bot_id != (route.source_bot_id if side == "source" else route.target_bot_id)
        or item.target.get("chat_id") != route.chat_id
        or item.payload.get("_mention_open_id")
        != meta.get("target_open_id" if side == "source" else "source_open_id")
    ):
        raise ValueError("stale_setup_probe")


def verified_identity(
    event: InboundEvent, *, message_id: str, chat_id: str, app_id: str, open_id: str
) -> tuple[str, str]:
    raw = event.payload.get("raw") or {}
    header, ev = raw.get("header") or {}, raw.get("event") or {}
    sender, message = ev.get("sender") or {}, ev.get("message") or {}
    union = (sender.get("sender_id") or {}).get("union_id")
    tenant = header.get("tenant_key")
    mentions = message.get("mentions") or []
    if (
        event.platform != "feishu"
        or event.platform_msg_id != message_id
        or event.chat_id != chat_id
        or event.chat_type != "group"
        or not event.payload.get("mentions_bot")
        or header.get("app_id") != app_id
        or header.get("event_type") != "im.message.receive_v1"
        or sender.get("sender_type") != "bot"
        or not isinstance(union, str)
        or not union
        or not isinstance(tenant, str)
        or not tenant
        or message.get("message_id") != message_id
        or message.get("chat_id") != chat_id
        or message.get("chat_type") != "group"
        or message.get("message_type") != "post"
        or not any(
            isinstance(m, dict) and (m.get("id") or {}).get("open_id") == open_id for m in mentions
        )
    ):
        raise ValueError("probe_identity_mismatch")
    return tenant, union


async def reconcile(session: AsyncSession, route: BotCollaborationRoute) -> None:
    meta = route.setup or {}
    if meta.get("status") != "pending":
        return
    reason: str | None = None
    try:
        await _current(session, route)
        if _expired(meta):
            raise ValueError("verification_expired")
        identities = []
        for side, receiver, app, own_open in (
            ("source", route.target_bot_id, meta["target_app_id"], meta["target_open_id"]),
            ("target", route.source_bot_id, meta["source_app_id"], meta["source_open_id"]),
        ):
            item = await session.get(
                OutboxItem, meta.get(f"{side}_outbox_id"), populate_existing=True
            )
            if not item or item.status in {"failed", "skipped"}:
                raise ValueError("probe_delivery_failed")
            mid = item.payload.get("_feishu_message_id")
            if item.status != "sent" or not mid:
                return
            event = await session.scalar(
                select(InboundEvent).where(
                    InboundEvent.bot_id == receiver, InboundEvent.platform_msg_id == mid
                )
            )
            if event is None:
                return
            identities.append(
                verified_identity(
                    event, message_id=mid, chat_id=route.chat_id, app_id=app, open_id=own_open
                )
            )
        if identities[0][0] != identities[1][0] or identities[0][1] == identities[1][1]:
            raise ValueError("probe_identity_mismatch")
        route.tenant_key = identities[0][0]
        route.source_union_id, route.target_union_id = identities[0][1], identities[1][1]
        route.setup = {**meta, "status": "ready", "reason": None}
    except (ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "invalid_setup_probe"
    if reason:
        route.setup = {
            **meta,
            "status": "expired" if reason == "verification_expired" else "failed",
            "reason": reason,
        }


async def status(session: AsyncSession, route: BotCollaborationRoute) -> dict[str, Any]:
    await reconcile(session, route)
    meta = route.setup or {}
    state, reason = meta.get("status", "pending"), meta.get("reason")
    if state in {"ready", "pending"}:
        try:
            await _current(session, route)
        except ValueError as exc:
            state, reason = "unavailable", str(exc)
    return {
        "status": state,
        "reason": reason,
        "can_enable": state == "ready" and not route.archived,
    }
