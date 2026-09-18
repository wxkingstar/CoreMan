"""扫码创建企业微信智能机器人的会话：发起、服务端轮询、校验、交付凭证。

- scode 与二维码链接加密落库，只给发起人本人看；拿到结果、过期或取消时立刻清空；
- 轮询只在服务端做：管理台的状态接口到点时查一次，scheduler 每 3 秒兜底扫一遍，发起人关掉
  页面后也能在有效期内尽快取回 Secret；服务重启后从库里接着轮询；
- 企业微信不会告诉你会话过期，二维码按 5 分钟自己计时，网络错误退避重试但不超出有效期；
- 取回的 Secret 只在服务端暂存，用长连接订阅校验后由「新建员工」一次性消费，不下发浏览器；
- 暂存期内没被消费的机器人可以在下次新建员工时继续使用，避免企业微信侧留下没人管的机器人。
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.crypto import Cipher
from coreman.core.db.models import User, WecomBotProvision
from coreman.core.errors import ApiError
from coreman.core.logging import get_logger
from coreman.core.observability.metrics import WECOM_PROVISIONS
from coreman.core.wecom_bots import scan, verify

QR_TTL = timedelta(minutes=5)
POLL_SECONDS = 3
MAX_BACKOFF_SECONDS = 30
SECRET_TTL = timedelta(hours=24)
# 暂存期内再次建员工时，超过这个时间的校验结果要重新确认一次。
VERIFY_FRESH = timedelta(minutes=10)
STARTS_PER_HOUR = 10
SWEEP_LIMIT = 50

log = get_logger(__name__)


def _aad(row: WecomBotProvision, field: str) -> str:
    return f"wecom_bot_provisions.{field}:{row.id}:{row.user_id}"


def _now() -> datetime:
    return datetime.now(UTC)


def _clear(row: WecomBotProvision, status: str, error: str | None = None) -> None:
    row.status = status
    row.pending_enc = None
    row.next_poll_at = None
    if status != "succeeded":
        row.secret_enc = None
    row.error = error


def _count(stage: str, result: str) -> None:
    WECOM_PROVISIONS.labels(stage, result).inc()


async def _rate_limit(session: AsyncSession, user: User) -> None:
    started = await session.scalar(
        select(func.count())
        .select_from(WecomBotProvision)
        .where(
            WecomBotProvision.user_id == user.id,
            WecomBotProvision.created_at > _now() - timedelta(hours=1),
        )
    )
    if (started or 0) >= STARTS_PER_HOUR:
        raise ApiError(429, 429, "扫码创建过于频繁，请稍后再试")


async def reusable(session: AsyncSession, user: User) -> WecomBotProvision | None:
    """本人扫码创建成功、还没被任何员工使用的机器人。"""
    row: WecomBotProvision | None = await session.scalar(
        select(WecomBotProvision)
        .where(
            WecomBotProvision.user_id == user.id,
            WecomBotProvision.status == "succeeded",
            WecomBotProvision.expires_at > _now(),
        )
        .order_by(WecomBotProvision.created_at.desc())
        .limit(1)
    )
    return row


async def start(session: AsyncSession, cipher: Cipher, user: User) -> WecomBotProvision:
    """生成二维码。企业微信出错时返回 failed 行：调用方提交后再报错，失败也计入限流与告警。"""
    await _rate_limit(session, user)
    # 每人同一时间只保留一个进行中的二维码。
    await session.execute(
        update(WecomBotProvision)
        .where(WecomBotProvision.user_id == user.id, WecomBotProvision.status == "pending")
        .values(status="cancelled", pending_enc=None, next_poll_at=None, error="superseded")
    )
    now = _now()
    row = WecomBotProvision(
        id=uuid.uuid4(),
        user_id=user.id,
        status="pending",
        expires_at=now + QR_TTL,
        poll_interval=POLL_SECONDS,
        next_poll_at=now + timedelta(seconds=POLL_SECONDS),
    )
    try:
        generated = await scan.generate()
    except scan.ScanError as exc:
        _count("generate", exc.code)
        if exc.code == "unexpected_response":
            log.error("wecom_provision_unexpected_response", stage="generate")
        row.status, row.expires_at, row.next_poll_at, row.error = "failed", now, None, exc.code
    else:
        _count("generate", "ok")
        row.pending_enc = cipher.encrypt(
            json.dumps({"scode": generated.scode, "auth_url": generated.auth_url}),
            _aad(row, "pending_enc"),
        )
    session.add(row)
    await session.flush()
    return row


async def load(
    session: AsyncSession, user: User, provision_id: uuid.UUID, *, lock: bool = True
) -> WecomBotProvision:
    query = select(WecomBotProvision).where(WecomBotProvision.id == provision_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    # 二维码等于密钥：只有发起人能看到二维码与结果，别人的会话一律按不存在处理。
    if row is None or row.user_id != user.id:
        raise ApiError(404, 404, "扫码会话不存在")
    return row


def _backoff(row: WecomBotProvision, now: datetime) -> int:
    row.poll_interval = min(MAX_BACKOFF_SECONDS, row.poll_interval * 2)
    row.next_poll_at = min(now + timedelta(seconds=row.poll_interval), row.expires_at)
    return row.poll_interval


async def refresh(cipher: Cipher, row: WecomBotProvision) -> int | None:
    """到点才向企业微信查一次；返回建议的下次查询间隔（秒）。调用方须已锁住该行。"""
    now = _now()
    if row.status == "succeeded" and row.expires_at <= now:
        _clear(row, "expired", "secret_expired")
        return None
    if row.status != "pending":
        return None
    if row.expires_at <= now or not row.pending_enc:
        _clear(row, "expired", "qr_expired")
        _count("session", "expired")
        return None
    if row.next_poll_at and row.next_poll_at > now:
        return max(1, math.ceil((row.next_poll_at - now).total_seconds()))
    pending = json.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")))
    try:
        result = await scan.query(pending["scode"])
    except scan.ScanError as exc:
        _count("query", exc.code)
        if exc.code == "unexpected_response" and row.error != exc.code:
            # 同一个会话只报一次；告警规则按最近出现过的会话计数。
            log.error(
                "wecom_provision_unexpected_response", stage="query", provision_id=str(row.id)
            )
            row.error = exc.code
        return _backoff(row, now)
    row.upstream_status = result.upstream_status
    if result.status == "waiting":
        row.poll_interval = POLL_SECONDS
        row.next_poll_at = now + timedelta(seconds=POLL_SECONDS)
        row.error = None
        return POLL_SECONDS
    # 拿到凭证就删掉本地的 scode：企业微信侧成功后仍能用它反复取回 Secret，CoreMan 作废不了，
    # 少存一份就少一个泄露点。后续只按这一行交付，重复处理不会再取一次。
    row.wecom_bot_id = result.bot_id
    row.secret_enc = cipher.encrypt(result.secret, _aad(row, "secret_enc"))
    row.expires_at = now + SECRET_TTL
    _clear(row, "succeeded")
    _count("session", "succeeded")
    log.info("wecom_provision_succeeded", provision_id=str(row.id))
    await check(cipher, row)
    return None


async def check(cipher: Cipher, row: WecomBotProvision) -> bool:
    """用长连接订阅校验暂存的凭证，结果记在行上；校验完立即断开，由网关接管。"""
    if row.status != "succeeded" or not row.secret_enc or not row.wecom_bot_id:
        return False
    secret = cipher.decrypt(row.secret_enc, _aad(row, "secret_enc"))
    try:
        await verify.verify_credentials(row.wecom_bot_id, secret)
    except verify.VerifyError as exc:
        _count("verify", exc.code)
        log.warning(
            "wecom_provision_verify_failed",
            provision_id=str(row.id),
            reason=exc.code,
            errcode=exc.errcode,
        )
        row.verified_at = None
        row.error = f"verify_{exc.code}"
        return False
    _count("verify", "ok")
    row.verified_at = _now()
    row.error = None
    return True


def credentials(cipher: Cipher, row: WecomBotProvision) -> dict[str, str]:
    """交付给机器人的凭证；调用方须已锁住该行，校验通过后调用 consume。"""
    if row.status != "succeeded" or not row.secret_enc or not row.wecom_bot_id:
        raise ApiError(422, 422, "企业微信机器人尚未创建完成或已失效，请重新扫码")
    if row.expires_at <= _now():
        _clear(row, "expired", "secret_expired")
        raise ApiError(422, 422, "企业微信机器人凭证暂存已过期，请重新扫码")
    return {
        "bot_id": row.wecom_bot_id,
        "secret": cipher.decrypt(row.secret_enc, _aad(row, "secret_enc")),
    }


async def ensure_verified(cipher: Cipher, row: WecomBotProvision) -> None:
    if row.verified_at and _now() - row.verified_at < VERIFY_FRESH:
        return
    if not await check(cipher, row):
        raise verify_failure(row.error)


def verify_failure(reason: str | None, errcode: int | None = None) -> ApiError:
    if reason == "verify_rejected":
        suffix = f"（errcode={errcode}）" if errcode is not None else ""
        return ApiError(
            422,
            422,
            f"企业微信拒绝了这组 Bot ID 与 Secret{suffix}。请核对凭证，并确认机器人已开启"
            "「API 模式」的长连接",
        )
    return ApiError(502, 502, "暂时无法连接企业微信校验凭证，请稍后重试")


def consume(row: WecomBotProvision, bot_id: uuid.UUID) -> None:
    row.bot_id = bot_id
    _clear(row, "consumed")


def cancel(row: WecomBotProvision) -> None:
    if row.status in ("pending", "succeeded"):
        if row.status == "pending":
            _count("session", "cancelled")
        _clear(row, "cancelled", "cancelled")


def out(cipher: Cipher, row: WecomBotProvision, retry_after: int | None = None) -> dict[str, Any]:
    url = None
    if row.status == "pending" and row.pending_enc:
        url = json.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc"))).get("auth_url")
    delivered = row.status in ("succeeded", "consumed")
    return {
        "id": str(row.id),
        "status": row.status,
        "bot_id": str(row.bot_id) if row.bot_id else None,
        "wecom_bot_id": row.wecom_bot_id if delivered else None,
        # 二维码内容；只有发起人在二维码有效期内拿得到。
        "url": url if isinstance(url, str) else None,
        "upstream_status": row.upstream_status if row.status == "pending" else None,
        "verified": bool(row.verified_at) if delivered else False,
        "expires_at": row.expires_at.isoformat(),
        "retry_after": retry_after,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def sweep(factory: async_sessionmaker[AsyncSession], cipher: Cipher) -> dict[str, int]:
    """scheduler 兜底：替离开页面的发起人把到点的会话查一遍，并清掉过期的暂存密钥。

    一行一个事务、跳过别人锁着的行：管理台正在轮询的会话不会被重复查询，
    单个会话的网络等待也不会拖住其它行。
    """
    polled = 0
    for _ in range(SWEEP_LIMIT):
        async with factory() as session:
            row = await session.scalar(
                select(WecomBotProvision)
                .where(
                    WecomBotProvision.status == "pending",
                    or_(
                        WecomBotProvision.next_poll_at.is_(None),
                        WecomBotProvision.next_poll_at <= _now(),
                    ),
                )
                .order_by(WecomBotProvision.next_poll_at.nulls_first())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
            await refresh(cipher, row)
            await session.commit()
            polled += 1
    async with factory() as session:
        result = await session.execute(
            update(WecomBotProvision)
            .where(WecomBotProvision.status == "succeeded", WecomBotProvision.expires_at <= _now())
            .values(status="expired", secret_enc=None, error="secret_expired")
        )
        await session.commit()
    return {"wecom_provisions_polled": polled, "wecom_secrets_expired": result.rowcount or 0}  # type: ignore[attr-defined]
