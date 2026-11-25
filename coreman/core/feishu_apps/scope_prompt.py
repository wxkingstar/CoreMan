"""管理员私聊自己的 AI 员工时，顺带看它的飞书应用缺不缺平台要的权限；缺就发一张卡片，点一下补齐。

平台加了新能力、清单里多了权限之后，不用再逐个通知各员工的管理员去扫码：

- 只提醒机器人的创建者和管理员（bot_members），只在他们和机器人的私聊里；
- 每个进程对每个机器人最多 10 分钟问一次飞书；同一组缺口每人每天最多一张卡片；
- 点「补齐权限」才发起扫码会话（飞书确认链接 10 分钟内有效，计入每人每小时的发起次数），
  确认结果由 scheduler 替发起人取回（`sweep`），写回这张卡片；密钥若有轮换也一并写回机器人。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.audit import record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import outbox
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuAppRegistration, InboundEvent, User
from coreman.core.errors import ApiError
from coreman.core.feishu_apps import management
from coreman.core.feishu_apps import service as registrations
from coreman.core.knowledge import installation
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.timeutils import utcnow

EVENT_KEY = "feishu_app_scopes"
CHECK_SECONDS = 600.0
POLL_SECONDS = 3
SWEEP_LIMIT = 20
SHOWN = 6
_DONE = "飞书那边已经确认，权限补上了，一般几分钟内生效。之后平台再加权限，你私聊它时还会这样提醒。"
_FAILED = {
    "denied": "你在飞书里取消了确认，权限没有补。",
    "expired": "确认链接过期了（10 分钟内有效），权限没有补。",
    "app_mismatch": "确认的不是这个员工绑定的飞书应用，权限没有补。",
    "not_admin": "你已经不是这个 AI 员工的管理员，没有替你补。",
}
_FAILED_DEFAULT = (
    "飞书没有完成补齐。通常是因为你不是这个飞书应用的管理员（扫码创建它的人默认是），"
    "可以请应用管理员私聊它来点，或在飞书开发者后台补。"
)

# bot_id, app_id → （过期时刻, 缺的权限；None 表示没查到）。
_checked: dict[tuple[uuid.UUID, str], tuple[float, tuple[str, ...] | None]] = {}


async def fetch_missing(app_id: str, secret: str, *, observe: bool) -> list[str] | None:
    """问飞书这个应用缺哪些清单权限；查不到返回 None。测试会替换它。"""
    client = FeishuClient(app_id, secret)
    try:
        return await management.missing(client, observe=observe)
    except (FeishuError, httpx.HTTPError):
        return None
    finally:
        await client.aclose()


def _credentials(cipher: Cipher, bot: Bot) -> tuple[str, str] | None:
    creds = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    app_id, secret = creds.get("app_id"), creds.get("app_secret")
    return (app_id, secret) if app_id and secret else None


async def _missing(cipher: Cipher, bot: Bot, *, fresh: bool = False) -> tuple[str, ...] | None:
    creds = _credentials(cipher, bot)
    if creds is None:
        return None
    key, now = (bot.id, creds[0]), time.monotonic()
    hit = _checked.get(key)
    if hit is not None and hit[0] > now and not fresh:
        return hit[1]
    found = await fetch_missing(*creds, observe=bot.group_observe != "off")
    result = tuple(found) if found is not None else None
    _checked[key] = (now + CHECK_SECONDS, result)
    return result


async def _admin(session: AsyncSession, bot: Bot, user_id: uuid.UUID | None) -> User | None:
    user = await session.get(User, user_id) if user_id else None
    if user is None or not await installation.bot_admin(session, bot, user):
        return None
    return user


async def offer(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    user_id: uuid.UUID | None,
    chat_id: str,
    task_id: int,
) -> bool:
    """私聊里说话的是这个员工的管理员、应用又缺权限：发一张补齐卡片。调用方提交事务。"""
    if bot.platform != "feishu" or not bot.enabled or not chat_id:
        return False
    user = await _admin(session, bot, user_id)
    if user is None:
        return False
    missing = await _missing(cipher, bot)
    if not missing:
        return False
    digest = hashlib.sha256(",".join(missing).encode()).hexdigest()[:12]
    added = await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        # 同一组缺口每人每天一张；平台再加权限（缺口变了）当天就会再提醒。
        dedupe_key=f"feishu-app-scopes:{bot.id}:{user.id}:{utcnow().date()}:{digest}",
        target={"chat_id": chat_id},
        payload={"card": offer_card(task_id, bot.name, missing)},
    )
    return added is not None


def _button(task_id: str, label: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [{"type": "callback", "value": {"task_id": task_id, "event_key": EVENT_KEY}}],
    }


def _card(text: str, *elements: dict[str, Any], note: str | None = None) -> dict[str, Any]:
    body: list[dict[str, Any]] = [{"tag": "markdown", "content": text}, *elements]
    if note:
        body.append({"tag": "markdown", "content": f"<font color='grey'>{note}</font>"})
    return {
        "schema": "2.0",
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "飞书应用缺少权限"},
        },
        "body": {"elements": body},
    }


def offer_card(task_id: int, bot_name: str, missing: Sequence[str]) -> dict[str, Any]:
    card_task = f"personal:{task_id}"
    shown = "、".join(missing[:SHOWN]) + (f" 等 {len(missing)} 项" if len(missing) > SHOWN else "")
    return {
        **_card(
            f"**{bot_name}** 的飞书应用还缺 {len(missing)} 项权限，用到相关能力时会失败。"
            "你是它的管理员，点下面的按钮，在飞书里确认一次就能补齐，确认后自动生效。",
            _button(card_task, "补齐权限"),
            note=f"缺少：{shown}\n需要你也是这个飞书应用的管理员（扫码创建它的人默认是）。",
        ),
        "task_id": card_task,
    }


async def _patch(
    session: AsyncSession,
    bot_id: uuid.UUID,
    target: dict[str, Any],
    key: str,
    text: str,
    *,
    url: str | None = None,
    retry: str | None = None,
) -> None:
    elements: list[dict[str, Any]] = []
    if url:
        elements.append(
            {
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "去飞书确认"},
                "behaviors": [{"type": "open_url", "default_url": url}],
            }
        )
    if retry:
        elements.append(_button(retry, "重新补齐"))
    await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="card_update",
        dedupe_key=key,
        target={"chat_id": target.get("chat_id"), "message_id": target.get("message_id")},
        payload={"card": _card(text, *elements)},
    )


async def handle(
    session: AsyncSession, cipher: Cipher, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """「补齐权限」的回调；调用方（`CardActionHandler`）已核对卡片由我们发到这个会话。

    只认这个员工的管理员点的；点了才发起扫码会话，把飞书确认链接换到卡片上。
    """
    card_task = str(action.get("task_id") or "")
    target = {
        "chat_id": inbound.chat_id,
        "message_id": str(inbound.reply_context.get("message_id") or ""),
        "task_id": card_task,
    }
    if bot.platform != "feishu" or not target["message_id"]:
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    user = await _admin(session, bot, speaker.user_id)
    if user is None:
        return "ignored"
    key = f"{inbound.id}:card_update"
    creds = _credentials(cipher, bot)
    if creds is None:
        await _patch(session, bot.id, target, key, "这个员工还没有关联飞书应用。")
        return "feishu_app_scopes_unbound"
    if await _missing(cipher, bot, fresh=True) == ():
        await _patch(session, bot.id, target, key, "已经补齐了，不用再点。")
        return "feishu_app_scopes_complete"
    try:
        row = await registrations.start(
            session, cipher, user, purpose="update", bot=bot, app_id=creds[0]
        )
    except ApiError as exc:
        await _patch(session, bot.id, target, key, f"{exc.message}。", retry=card_task)
        return "feishu_app_scopes_unavailable"
    row.notify_card = target
    await record_audit(
        session,
        action="feishu_app.registration_start",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="feishu_app_registration",
        target_id=str(row.id),
        diff={"purpose": [None, "update"], "bot_id": [None, str(bot.id)], "via": [None, "card"]},
    )
    await _patch(
        session,
        bot.id,
        target,
        key,
        "点下面的按钮，在飞书里确认一次（10 分钟内有效）。确认后这里会告诉你结果。",
        url=registrations.pending_url(cipher, row),
    )
    return "feishu_app_scopes_started"


async def _advance(session: AsyncSession, cipher: Cipher, row: FeishuAppRegistration) -> None:
    await registrations.refresh(cipher, row)
    if row.status == "pending":
        return
    assert row.bot_id is not None and row.notify_card  # 卡片发起的补齐总带着机器人和卡片
    card: dict[str, Any] = row.notify_card
    if row.status == "succeeded":
        bot = await session.scalar(select(Bot).where(Bot.id == row.bot_id).with_for_update())
        user = await _admin(session, bot, row.user_id) if bot is not None else None
        if bot is None or user is None:
            registrations.cancel(row)
            row.error = "not_admin"
        else:
            changed = registrations.apply_update(cipher, row, bot)
            if row.status == "consumed":
                detail: dict[str, Any] = {"app_id": [row.app_id, row.app_id]}
                if changed:
                    detail["credentials"] = ["***", "***"]
                    await notify_bot_changed(session, bot.id)
                await record_audit(
                    session,
                    action="bot.feishu_app_update",
                    actor_id=user.id,
                    actor_login=user.login_name,
                    target_type="bot",
                    target_id=str(bot.id),
                    diff=detail,
                )
    done = row.status == "consumed"
    text = _DONE if done else _FAILED.get(row.status, _FAILED.get(row.error or "", _FAILED_DEFAULT))
    await _patch(
        session,
        row.bot_id,
        card,
        f"feishu-app-scopes:{row.id}:result",
        text if done else f"{text}需要的话再点一次。",
        retry=None if done else str(card.get("task_id") or "") or None,
    )


async def sweep(factory: async_sessionmaker[AsyncSession], cipher: Cipher) -> dict[str, int]:
    """scheduler：替从卡片发起补齐的管理员取回结果。一行一个事务、跳过别人锁着的行。"""
    polled = 0
    for _ in range(SWEEP_LIMIT):
        async with factory() as session:
            now = utcnow()
            row = await session.scalar(
                select(FeishuAppRegistration)
                .where(
                    FeishuAppRegistration.notify_card.is_not(None),
                    FeishuAppRegistration.status.in_(("pending", "succeeded")),
                    or_(
                        FeishuAppRegistration.next_poll_at.is_(None),
                        FeishuAppRegistration.next_poll_at <= now,
                    ),
                )
                .order_by(FeishuAppRegistration.next_poll_at.nulls_first())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
            await _advance(session, cipher, row)
            await session.commit()
            polled += 1
    return {"feishu_app_scopes_polled": polled}
