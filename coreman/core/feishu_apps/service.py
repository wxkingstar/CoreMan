"""扫码注册会话：开始、按平台节奏轮询、交付凭证。

- 设备码与确认链接加密落库，多个 API 副本之间靠行锁串行轮询；
- 扫码成功后 App Secret 只在服务端暂存，由「新建员工」一次性消费，不下发浏览器；
- 暂存期内未被消费的应用可以在下次新建员工时继续使用，避免飞书侧留下无人管理的应用。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json, encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuAppRegistration, User
from coreman.core.errors import ApiError
from coreman.core.feishu_apps import registration

SECRET_TTL = timedelta(hours=24)
STARTS_PER_HOUR = 10
FINAL = ("consumed", "expired", "denied", "failed", "cancelled")


def _aad(row: FeishuAppRegistration, field: str) -> str:
    return f"feishu_app_registrations.{field}:{row.id}:{row.user_id}"


def _now() -> datetime:
    return datetime.now(UTC)


def _clear(row: FeishuAppRegistration, status: str, error: str | None = None) -> None:
    row.status = status
    row.pending_enc = None
    row.next_poll_at = None
    if status != "succeeded":
        row.secret_enc = None
    row.error = error


async def _rate_limit(session: AsyncSession, user: User) -> None:
    started = await session.scalar(
        select(func.count())
        .select_from(FeishuAppRegistration)
        .where(
            FeishuAppRegistration.user_id == user.id,
            FeishuAppRegistration.created_at > _now() - timedelta(hours=1),
        )
    )
    if (started or 0) >= STARTS_PER_HOUR:
        raise ApiError(429, 429, "扫码创建过于频繁，请稍后再试")


async def reusable(session: AsyncSession, user: User) -> FeishuAppRegistration | None:
    """本人扫码创建成功、还没被任何员工使用的应用。"""
    row: FeishuAppRegistration | None = await session.scalar(
        select(FeishuAppRegistration)
        .where(
            FeishuAppRegistration.user_id == user.id,
            FeishuAppRegistration.purpose == "create",
            FeishuAppRegistration.status == "succeeded",
            FeishuAppRegistration.expires_at > _now(),
        )
        .order_by(FeishuAppRegistration.created_at.desc())
        .limit(1)
    )
    return row


async def start(
    session: AsyncSession,
    cipher: Cipher,
    user: User,
    *,
    purpose: str,
    bot: Bot | None = None,
    app_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    avatar_url: str | None = None,
) -> FeishuAppRegistration:
    await _rate_limit(session, user)
    # 同一个人同一目标只保留一个进行中的二维码。
    await session.execute(
        update(FeishuAppRegistration)
        .where(
            FeishuAppRegistration.user_id == user.id,
            FeishuAppRegistration.purpose == purpose,
            FeishuAppRegistration.status == "pending",
            FeishuAppRegistration.bot_id.is_(None)
            if bot is None
            else FeishuAppRegistration.bot_id == bot.id,
        )
        .values(status="cancelled", pending_enc=None, next_poll_at=None)
    )
    try:
        began = await registration.begin(
            name=(name or "").strip()[:64] or None,
            desc=(description or "").strip()[:200] or None,
            avatars=[avatar_url] if avatar_url and avatar_url.startswith("https://") else None,
            app_id=app_id,
        )
    except registration.RegistrationError as exc:
        raise ApiError(502, 502, f"飞书暂时无法创建应用（{exc.code}）") from exc
    row = FeishuAppRegistration(
        id=uuid.uuid4(),
        user_id=user.id,
        bot_id=bot.id if bot else None,
        purpose=purpose,
        status="pending",
        expires_at=_now() + timedelta(seconds=began.expires_in),
        poll_interval=began.interval,
        app_id=app_id,
    )
    row.pending_enc = cipher.encrypt(
        json.dumps({"device_code": began.device_code, "url": began.url}),
        _aad(row, "pending_enc"),
    )
    session.add(row)
    await session.flush()
    return row


async def load(
    session: AsyncSession, user: User, registration_id: uuid.UUID, *, lock: bool = True
) -> FeishuAppRegistration:
    query = select(FeishuAppRegistration).where(FeishuAppRegistration.id == registration_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    # 只有发起人能看到二维码与结果；别人的会话一律按不存在处理。
    if row is None or row.user_id != user.id:
        raise ApiError(404, 404, "扫码会话不存在")
    return row


async def refresh(cipher: Cipher, row: FeishuAppRegistration) -> int | None:
    """到点才向飞书轮询一次；返回建议的下次轮询间隔（秒）。调用方须已锁住该行。"""
    now = _now()
    if row.status == "succeeded" and row.expires_at <= now:
        _clear(row, "expired", "secret_expired")
        return None
    if row.status != "pending":
        return None
    if row.expires_at <= now or not row.pending_enc:
        _clear(row, "expired", "qr_expired")
        return None
    if row.next_poll_at and row.next_poll_at > now:
        return max(1, int((row.next_poll_at - now).total_seconds()))
    pending = json.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")))
    row.next_poll_at = now + timedelta(seconds=row.poll_interval)
    try:
        result = await registration.poll(pending["device_code"])
    except registration.RegistrationError:
        # 网络抖动不终止会话，按原节奏继续等。
        return row.poll_interval
    if result.status == "pending":
        return row.poll_interval
    if result.status == "slow_down":
        row.poll_interval = min(60, row.poll_interval + 5)
        row.next_poll_at = now + timedelta(seconds=row.poll_interval)
        return row.poll_interval
    if result.status != "succeeded":
        status = result.status if result.status in ("expired", "denied") else "failed"
        _clear(row, status, result.status)
        return None
    if row.purpose == "update" and row.app_id and result.app_id != row.app_id:
        _clear(row, "failed", "app_mismatch")
        return None
    row.app_id = result.app_id
    row.owner_open_id = result.open_id or None
    row.secret_enc = cipher.encrypt(result.app_secret, _aad(row, "secret_enc"))
    row.expires_at = now + SECRET_TTL
    _clear(row, "succeeded")
    return None


def pending_url(cipher: Cipher, row: FeishuAppRegistration) -> str | None:
    if row.status != "pending" or not row.pending_enc:
        return None
    data = json.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")))
    url = data.get("url")
    return url if isinstance(url, str) else None


def credentials(cipher: Cipher, row: FeishuAppRegistration) -> dict[str, str]:
    """交付给机器人的凭证；调用方须已锁住该行并随后调用 consume。"""
    if row.status != "succeeded" or not row.secret_enc or not row.app_id:
        raise ApiError(422, 422, "飞书应用尚未创建完成或已失效，请重新扫码")
    if row.expires_at <= _now():
        _clear(row, "expired", "secret_expired")
        raise ApiError(422, 422, "飞书应用凭证暂存已过期，请重新扫码")
    return {
        "app_id": row.app_id,
        "app_secret": cipher.decrypt(row.secret_enc, _aad(row, "secret_enc")),
    }


def consume(row: FeishuAppRegistration, bot_id: uuid.UUID) -> None:
    row.bot_id = bot_id
    _clear(row, "consumed")


def cancel(row: FeishuAppRegistration) -> None:
    if row.status in ("pending", "succeeded"):
        _clear(row, "cancelled", "cancelled")


def apply_update(cipher: Cipher, row: FeishuAppRegistration, bot: Bot) -> bool:
    """补齐权限成功后把可能轮换的密钥写回机器人；密钥没变就不改，免得网关白白重连。

    扫码期间机器人换了应用时只把会话标记为失败（由调用方提交），不改机器人。
    """
    new = credentials(cipher, row)
    current = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    if current.get("app_id") != new["app_id"]:
        _clear(row, "failed", "app_mismatch")
        return False
    changed = current.get("app_secret") != new["app_secret"]
    if changed:
        # version 是 ORM 乐观锁列，写入时自动 +1。
        bot.credentials_enc = encrypt_json(cipher, {**current, **new}, CREDENTIALS_AAD)
    consume(row, bot.id)
    return changed


def out(
    cipher: Cipher, row: FeishuAppRegistration, retry_after: int | None = None
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "purpose": row.purpose,
        "status": row.status,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "app_id": row.app_id if row.status in ("succeeded", "consumed") else None,
        "url": pending_url(cipher, row),
        "expires_at": row.expires_at.isoformat(),
        "retry_after": retry_after,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
