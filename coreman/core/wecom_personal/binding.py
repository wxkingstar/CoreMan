"""扫码绑定：本人用企业微信扫码建一个授权机器人，CoreMan 取回凭证、核对是本人后托管。

扫码协议与「扫码创建 AI 员工」相同（见 wecom_bots.scan），区别在交付：这里不建员工、不连长连接，
而是用凭证换网关令牌、查 whoami，确认授权人就是发起扫码的本人，再逐项试一次只读调用。

- scode 与二维码链接加密保存，只给发起人本人；拿到结果、过期或取消即清空；
- 轮询在服务端做：页面到点查一次，scheduler 每 3 秒兜底，本人离开页面也能尽快完成绑定；
- 扫到凭证但暂时连不上企业微信核对时，保留会话继续重试（同一个 scode 会返回同一份凭证），
  直到宽限期结束；
- 别人扫了本人的二维码：凭证代表的是扫码人，不是本人，直接丢弃。
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import outbox
from coreman.core.chat.identity import resolve_speaker
from coreman.core.chat.openuserid import OpenUseridResolver
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, WecomPersonalBinding
from coreman.core.logging import get_logger
from coreman.core.wecom_bots import scan
from coreman.core.wecom_personal import gateway, service
from coreman.core.wecom_personal.cards import card_task_id, selection_card

QR_TTL = timedelta(minutes=5)
POLL_SECONDS = 3
MAX_BACKOFF_SECONDS = 30
# 扫到凭证后，核对本人的重试最多再延长这么久。
VERIFY_GRACE = timedelta(minutes=10)
SWEEP_LIMIT = 50
# 私聊里发起绑定后这么久之内完成，结果才回到那个私聊。
NOTIFY_TTL = timedelta(minutes=30)
# 绑定没成功时回到私聊的说明；二维码过期不打扰，本人重新点链接即可。
ANNOUNCED_FAILURES = {
    "not_self": (
        "确认授权的不是你本人的企业微信账号，没有绑定。请用你自己的企业微信重新点授权链接。"
    ),
    "identity_unlinked": (
        "企业微信返回的账号在 CoreMan 里对不上，没有绑定，请联系管理员同步通讯录。"
    ),
}
# 取消或结束后，这么久之内不再重新生成：二维码要向企业微信申请，别被反复点击刷接口。
RESTART_COOLDOWN = timedelta(seconds=10)
# 扫码失败的原因（scan_error）：前端按这些键给出说明。
FAILURES = (
    "not_self",
    "identity_unlinked",
    "not_authorized",
    "identity_mismatch",
    "credentials_rejected",
    "qr_expired",
    "verify_timeout",
    "upstream_unavailable",
    "unexpected_response",
    "cancelled",
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _clear(row: WecomPersonalBinding, status: str, error: str | None = None) -> None:
    row.scan_status = status
    row.scan_enc = None
    row.scan_next_poll_at = None
    row.scan_error = error


def pending(row: WecomPersonalBinding | None) -> bool:
    return row is not None and row.scan_status == "pending"


async def start(cipher: Cipher, row: WecomPersonalBinding) -> None:
    """生成二维码；有效期内的二维码直接复用，避免反复生成。"""
    now = _now()
    if row.scan_status == "pending" and row.scan_expires_at and row.scan_expires_at > now:
        return
    started = row.scan_expires_at - QR_TTL if row.scan_expires_at else None
    if started is not None and now - started < RESTART_COOLDOWN:
        raise service.PersonalError("too_frequent")
    try:
        generated = await scan.generate()
    except scan.ScanError as exc:
        if exc.code == "unexpected_response":
            log.error("wecom_personal_scan_unexpected_response", stage="generate")
        _clear(row, "failed", exc.code)
        row.scan_expires_at = now
        return
    row.scan_status = "pending"
    row.scan_enc = cipher.encrypt(
        json.dumps({"scode": generated.scode, "auth_url": generated.auth_url}),
        service.aad(row, "scan_enc"),
    )
    row.scan_expires_at = now + QR_TTL
    row.scan_poll_interval = POLL_SECONDS
    row.scan_next_poll_at = now + timedelta(seconds=POLL_SECONDS)
    row.scan_upstream_status = None
    row.scan_error = None


def remember_chat(
    row: WecomPersonalBinding, *, bot_id: uuid.UUID, chat_id: str, task_id: int
) -> None:
    """本人在私聊里要求绑定：记下来，绑定结果回到这个私聊。"""
    row.scan_notify = {
        "bot_id": str(bot_id),
        "chat_id": chat_id,
        "task_id": task_id,
        "at": _now().isoformat(),
    }


def _success_text(row: WecomPersonalBinding) -> str:
    summary = service.summary(row)
    lines = [
        "**已绑定企业微信**",
        f"授权机器人「{row.bot_name or row.wecom_bot_id}」代表你本人，现在的使用范围是"
        f"「{service.LEVEL_TITLES.get(row.authorization_level, '仅读取')}」。",
    ]
    observed = [
        entry
        for key, entry in (row.capabilities or {}).items()
        if key.endswith(":read") and isinstance(entry, dict)
    ]
    if observed and all(entry.get("state") == "unauthorized" for entry in observed):
        lines.append(
            "不过企业微信里还没有授权任何能力，可能是跳过了「确认授权」。" + service.renew_hint(row)
        )
    elif summary["unauthorized"] or summary["expired"]:
        missing = "、".join(summary["unauthorized"] + summary["expired"])
        lines.append(f"其中「{missing}」暂时用不了。" + service.renew_hint(row))
    lines.append("现在就可以直接问我，例如“看看我这周的日程”；要调整范围，在下面的卡片里选择。")
    return "\n".join(lines)


async def _announce(session: AsyncSession, row: WecomPersonalBinding, outcome: str) -> None:
    """把绑定结果发回发起绑定的那个私聊；成功时附上档位卡片。"""
    notify, row.scan_notify = row.scan_notify, None
    if not isinstance(notify, dict):
        return
    try:
        at = datetime.fromisoformat(str(notify["at"]))
        bot_id = uuid.UUID(str(notify["bot_id"]))
        chat_id = str(notify["chat_id"])
        task_id = int(notify["task_id"])
    except (KeyError, TypeError, ValueError):
        return
    if _now() - at > NOTIFY_TTL:
        return
    bot = await session.get(Bot, bot_id)
    if bot is None or not bot.enabled or bot.platform != "wecom":
        return
    if outcome == "bound":
        text = _success_text(row)
    elif outcome in ANNOUNCED_FAILURES:
        text = ANNOUNCED_FAILURES[outcome]
    else:
        text = "没有绑定成功，请重新发送“连接企业微信”再试一次。"
    stamp = int(_now().timestamp())
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key=f"wecom_personal:bind:{row.user_id}:{stamp}:text",
        target={"chat_id": chat_id},
        payload={"markdown": text},
    )
    if outcome == "bound":
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key=f"wecom_personal:bind:{row.user_id}:{stamp}:card",
            target={"chat_id": chat_id},
            payload={"card": selection_card(card_task_id(task_id), row.authorization_level)},
        )


def cancel(row: WecomPersonalBinding) -> None:
    if row.scan_status == "pending":
        _clear(row, "cancelled", "cancelled")


def _backoff(row: WecomPersonalBinding, now: datetime) -> int:
    assert row.scan_expires_at is not None
    row.scan_poll_interval = min(MAX_BACKOFF_SECONDS, row.scan_poll_interval * 2)
    row.scan_next_poll_at = min(
        now + timedelta(seconds=row.scan_poll_interval), row.scan_expires_at
    )
    return row.scan_poll_interval


async def _adopt(
    session: AsyncSession,
    cipher: Cipher,
    row: WecomPersonalBinding,
    bot_id: str,
    secret: str,
    resolver: OpenUseridResolver | None,
) -> str:
    """核对扫码得到的凭证代表的就是本人；是就换成这份凭证。返回 bound 或失败原因。"""
    try:
        token = await gateway.fetch_token(bot_id, secret)
        identity = await gateway.whoami(token)
    except gateway.GatewayError as exc:
        return "credentials_rejected" if exc.code == "credentials_rejected" else "retry"
    if identity.bot_id != bot_id:
        return "identity_mismatch"
    if not identity.authorizer_id:
        return "not_authorized"
    speaker = await resolve_speaker(
        session, platform="wecom", platform_user_id=identity.authorizer_id, resolver=resolver
    )
    if not speaker.known or speaker.user_id is None:
        return "identity_unlinked"
    if speaker.user_id != row.user_id:
        return "not_self"
    service.bind(row, cipher, bot_id=bot_id, secret=secret, token=token, identity=identity)
    return "bound"


async def refresh(
    session: AsyncSession,
    cipher: Cipher,
    row: WecomPersonalBinding,
    resolver: OpenUseridResolver | None = None,
) -> int | None:
    """到点才向企业微信查一次；返回建议的下次查询间隔（秒）。调用方须已锁住该行。"""
    if row.scan_status != "pending":
        return None
    now = _now()
    if not row.scan_enc or row.scan_expires_at is None or row.scan_expires_at <= now:
        confirmed = row.scan_upstream_status == "success"
        _clear(row, "expired", "verify_timeout" if confirmed else "qr_expired")
        return None
    if row.scan_next_poll_at and row.scan_next_poll_at > now:
        return max(1, math.ceil((row.scan_next_poll_at - now).total_seconds()))
    data = json.loads(cipher.decrypt(row.scan_enc, service.aad(row, "scan_enc")))
    try:
        result = await scan.query(data["scode"])
    except scan.ScanError as exc:
        if exc.code == "unexpected_response" and row.scan_error != exc.code:
            log.error("wecom_personal_scan_unexpected_response", stage="query")
            row.scan_error = exc.code
        return _backoff(row, now)
    if result.status == "waiting":
        row.scan_upstream_status = result.upstream_status
        row.scan_poll_interval = POLL_SECONDS
        row.scan_next_poll_at = now + timedelta(seconds=POLL_SECONDS)
        return POLL_SECONDS
    outcome = await _adopt(session, cipher, row, result.bot_id, result.secret, resolver)
    if outcome == "retry":
        # 凭证拿到了但暂时核对不了：保留 scode 重试，同一个 scode 会返回同一份凭证。
        if row.scan_upstream_status != "success":
            row.scan_upstream_status = "success"
            row.scan_expires_at = max(row.scan_expires_at, now + VERIFY_GRACE)
        row.scan_error = "upstream_unavailable"
        return _backoff(row, now)
    if outcome != "bound":
        log.warning("wecom_personal_scan_rejected", reason=outcome)
        _clear(row, "failed", outcome)
        await _announce(session, row, outcome)
        return None
    row.scan_upstream_status = "success"
    _clear(row, "succeeded")
    log.info("wecom_personal_bound")
    try:
        await service.probe(cipher, row)
    except service.PersonalError:
        # 检测失败不影响绑定：页面上可以再点一次「重新检查」。
        pass
    await _announce(session, row, "bound")
    return None


def scan_state(
    cipher: Cipher, row: WecomPersonalBinding | None, retry_after: int | None = None
) -> dict[str, Any] | None:
    """扫码会话的状态；二维码内容只在有效期内返回给本人。"""
    if row is None or row.scan_status is None:
        return None
    url = None
    if row.scan_status == "pending" and row.scan_enc:
        url = json.loads(cipher.decrypt(row.scan_enc, service.aad(row, "scan_enc"))).get("auth_url")
    result: dict[str, Any] = {
        "status": row.scan_status,
        "url": url if isinstance(url, str) else None,
        "upstream_status": row.scan_upstream_status if row.scan_status == "pending" else None,
        "expires_at": row.scan_expires_at.isoformat() if row.scan_expires_at else None,
        "error": row.scan_error,
        "retry_after": retry_after,
    }
    return result


async def sweep(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    resolver: OpenUseridResolver | None = None,
) -> int:
    """scheduler 兜底：替离开页面的本人把到点的扫码会话查一遍。一行一个事务，跳过锁着的行。"""
    polled = 0
    for _ in range(SWEEP_LIMIT):
        async with factory() as session:
            row = await session.scalar(
                select(WecomPersonalBinding)
                .where(
                    WecomPersonalBinding.scan_status == "pending",
                    or_(
                        WecomPersonalBinding.scan_next_poll_at.is_(None),
                        WecomPersonalBinding.scan_next_poll_at <= _now(),
                    ),
                )
                .order_by(WecomPersonalBinding.scan_next_poll_at.nulls_first())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
            await refresh(session, cipher, row, resolver)
            await session.commit()
            polled += 1
    return polled
