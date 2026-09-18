"""企业微信个人工具的授权状态与调用。

企业微信的「可使用权限」由成员在机器人编辑页授权，之后机器人代这位成员执行，调用里不带发言人。
所以连接时要向企业微信核对「授权人就是本人私聊的发送者」，此后每隔几分钟再核一次：
授权换了人、机器人换了凭证，本人的连接立刻作废，绝不拿别人的授权替本人干活。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import WecomPersonalGrant
from coreman.core.wecom_personal import gateway
from coreman.core.wecom_personal.policy import Scope, bot_credentials

if TYPE_CHECKING:
    from coreman.core.chat.openuserid import OpenUseridResolver

LEVELS = ("readonly", "all_except_send", "all")
LEVEL_TITLES = {
    "readonly": "仅读取",
    "all_except_send": "读写（不含发邮件、共享文档）",
    "all": "全部（含发邮件、共享文档）",
}
SELECTION_TTL = timedelta(minutes=10)
# 复核授权人的间隔：每次调用都核会多一倍请求，太久又会让换了授权人的机器人多干几轮活。
VERIFY_INTERVAL = timedelta(minutes=5)
AUTHORIZE_HINT = (
    "请在企业微信「工作台 → 智能机器人」中找到这个机器人，进入「可使用权限」为需要的能力授权"
    "（文档权限有效期 7 天，到期需重新授权）。"
)
RETENTION_NOTICE = (
    "企业微信的「可使用权限」绑定在机器人上：机器人代表在编辑页完成授权的那位成员操作，"
    "所以只有授权人本人能连接，只在你和机器人的私聊里生效，群聊不会使用；"
    "你本人创建、结果只发给你本人的定时任务也可以使用。"
    "连接期间的私聊与普通私聊一样保留在对话记录中，但只有你本人能查看，机器人管理员也看不到；"
    "断开连接会停止后续使用，但不会删除已有记录，也不会取消企业微信里的授权。"
)


class PersonalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _aad(row: WecomPersonalGrant, field: str) -> str:
    return f"wecom_personal_grants.{field}:{row.bot_id}:{row.user_id}"


def _fingerprint(bot_id: str, secret: str) -> str:
    return hashlib.sha256(f"{bot_id}:{secret}".encode()).hexdigest()


def _clear(row: WecomPersonalGrant, status: str = "revoked") -> None:
    row.context_epoch = uuid.uuid4()
    row.status = status
    row.token_enc = None
    row.authorizer_id = None
    row.verified_at = None
    row.selection_chat_id = None
    row.selection_task_id = None
    row.selection_expires_at = None


async def _lock(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> None:
    digest = hashlib.sha256(f"wecom-personal:{bot_id}:{user_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def existing_row(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID
) -> WecomPersonalGrant | None:
    """加锁读取，不为没连接过的人建记录。连接、复核、断开与调用都按这把锁串行。"""
    await _lock(session, bot_id, user_id)
    return (
        await session.scalars(
            select(WecomPersonalGrant)
            .where(WecomPersonalGrant.bot_id == bot_id, WecomPersonalGrant.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()


async def _row(session: AsyncSession, scope: Scope) -> WecomPersonalGrant:
    await _lock(session, scope.bot.id, scope.user_id)
    await session.execute(
        insert(WecomPersonalGrant)
        .values(
            bot_id=scope.bot.id,
            user_id=scope.user_id,
            status="revoked",
            authorization_level="readonly",
        )
        .on_conflict_do_nothing()
    )
    row = await existing_row(session, scope.bot.id, scope.user_id)
    assert row is not None
    return row


def _credentials(cipher: Cipher, scope: Scope) -> tuple[str, str]:
    try:
        return bot_credentials(cipher, scope.bot)
    except ValueError as exc:
        raise PersonalError("wecom_bot_unavailable") from exc


def _upstream(exc: gateway.GatewayError) -> PersonalError:
    if exc.code == "credentials_rejected":
        return PersonalError("wecom_bot_unavailable")
    return PersonalError("upstream_unavailable")


async def begin_selection(session: AsyncSession, scope: Scope) -> None:
    """本人发了「连接企业微信」：旧连接作废，等本人在卡片上选档位。"""
    row = await _row(session, scope)
    _clear(row, "selecting")
    row.selection_chat_id = scope.chat_id
    row.selection_task_id = scope.task.id
    row.selection_expires_at = datetime.now(UTC) + SELECTION_TTL


async def _same_member(
    session: AsyncSession, scope: Scope, authorizer_id: str, resolver: OpenUseridResolver | None
) -> bool:
    if authorizer_id in scope.sender_ids:
        return True
    # 授权人 ID 与回调里的发送者 ID 可能一个明文一个密文：落到同一位员工也算。
    speaker = await resolve_speaker(
        session, platform="wecom", platform_user_id=authorizer_id, resolver=resolver
    )
    return speaker.known and speaker.user_id == scope.user_id


async def _authorizer(cipher: Cipher, scope: Scope) -> tuple[str, gateway.Identity]:
    bot_id, secret = _credentials(cipher, scope)
    try:
        token = await gateway.fetch_token(bot_id, secret)
        return token, await gateway.whoami(token)
    except gateway.GatewayError as exc:
        raise _upstream(exc) from exc


async def _check(
    session: AsyncSession,
    scope: Scope,
    identity: gateway.Identity,
    bot_id: str,
    resolver: OpenUseridResolver | None,
) -> None:
    if identity.bot_id != bot_id:
        raise PersonalError("identity_mismatch")
    if not identity.authorizer_id:
        raise PersonalError("not_authorized")
    if not await _same_member(session, scope, identity.authorizer_id, resolver):
        raise PersonalError("not_authorizer")


async def precheck(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    *,
    resolver: OpenUseridResolver | None = None,
) -> None:
    """发卡片之前先问一次企业微信：不是授权人就直接说明，不发卡片、也不留授权记录。"""
    _, identity = await _authorizer(cipher, scope)
    await _check(session, scope, identity, _credentials(cipher, scope)[0], resolver)


async def choose(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    level: str,
    *,
    selection_task_id: int,
    resolver: OpenUseridResolver | None = None,
) -> dict[str, Any]:
    """本人在卡片上选了档位：向企业微信核对授权人，是本人才连接。"""
    row = await _row(session, scope)
    if (
        level not in LEVELS
        or row.status != "selecting"
        or row.selection_chat_id != scope.chat_id
        or row.selection_task_id != selection_task_id
        or not row.selection_expires_at
        or row.selection_expires_at <= datetime.now(UTC)
    ):
        raise PersonalError("selection_required")
    bot_id, secret = _credentials(cipher, scope)
    # 网络或企业微信暂时不可用时原样抛出：卡片仍然有效，本人可以再点一次。
    token, identity = await _authorizer(cipher, scope)
    try:
        await _check(session, scope, identity, bot_id, resolver)
    except PersonalError:
        _clear(row)
        raise
    assert identity.authorizer_id is not None
    row.context_epoch = uuid.uuid4()
    row.status = "connected"
    row.authorization_level = level
    row.authorizer_id = identity.authorizer_id
    row.verified_at = datetime.now(UTC)
    row.token_enc = cipher.encrypt(token, _aad(row, "token_enc"))
    row.bot_fingerprint = _fingerprint(bot_id, secret)
    row.selection_chat_id = row.selection_task_id = row.selection_expires_at = None
    return state(row)


async def _with_token[T](
    cipher: Cipher,
    row: WecomPersonalGrant,
    credentials: tuple[str, str],
    call: Callable[[str], Awaitable[T]],
) -> T:
    """用保存的令牌调用；令牌过期或无效就重新签名换一个，再试一次。"""
    bot_id, secret = credentials
    token = cipher.decrypt(row.token_enc, _aad(row, "token_enc")) if row.token_enc else None
    for attempt in range(2):
        if token is None:
            token = await gateway.fetch_token(bot_id, secret)
            row.token_enc = cipher.encrypt(token, _aad(row, "token_enc"))
        try:
            return await call(token)
        except gateway.GatewayError as exc:
            if exc.code != "token_expired" or attempt:
                raise
            token = None
    raise AssertionError("unreachable")


async def verify(
    session: AsyncSession, cipher: Cipher, row: WecomPersonalGrant, scope: Scope
) -> tuple[str, str]:
    """确认本人的连接仍然有效：机器人凭证没换、企业微信里的授权人仍是本人。"""
    if row.status != "connected" or not row.authorizer_id:
        raise PersonalError("authorization_required")
    credentials = _credentials(cipher, scope)
    fingerprint = _fingerprint(*credentials)
    now = datetime.now(UTC)
    if (
        row.bot_fingerprint == fingerprint
        and row.verified_at is not None
        and now - row.verified_at < VERIFY_INTERVAL
    ):
        return credentials
    if row.bot_fingerprint != fingerprint:
        # 凭证换了：旧令牌签给的是旧 Secret，重换并重新核对授权人。
        row.token_enc = None
        row.bot_fingerprint = fingerprint
    try:
        identity = await _with_token(cipher, row, credentials, gateway.whoami)
    except gateway.GatewayError as exc:
        raise _upstream(exc) from exc
    if identity.bot_id != credentials[0] or identity.authorizer_id != row.authorizer_id:
        _clear(row)
        raise PersonalError("authorization_changed")
    row.verified_at = now
    return credentials


async def call[T](
    session: AsyncSession,
    cipher: Cipher,
    row: WecomPersonalGrant,
    scope: Scope,
    invoke: Callable[[str], Awaitable[T]],
) -> T:
    """复核之后以授权人（本人）身份调用企业微信；业务错误原样抛给调用方处理。"""
    credentials = await verify(session, cipher, row, scope)
    try:
        return await _with_token(cipher, row, credentials, invoke)
    except gateway.GatewayError as exc:
        if exc.code == "wecom_error":
            raise
        raise _upstream(exc) from exc


async def revoke_grant(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """只断开 CoreMan 这一侧；企业微信里的授权要本人在机器人编辑页取消。"""
    row = await existing_row(session, bot_id, user_id)
    if row is not None:
        _clear(row)
    return {"status": "revoked"}


def state(row: WecomPersonalGrant) -> dict[str, Any]:
    now = datetime.now(UTC)
    status = row.status
    if status == "selecting" and (
        row.selection_expires_at is None or row.selection_expires_at <= now
    ):
        status = "expired"
    return {
        "status": status,
        "authorization_level": row.authorization_level,
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        "selection_expires_at": (
            row.selection_expires_at.isoformat()
            if status == "selecting" and row.selection_expires_at
            else None
        ),
        "retention_notice": RETENTION_NOTICE,
    }
