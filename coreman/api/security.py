"""管理台会话 cookie（签名的 admin_sessions.id）、双 cookie CSRF、登录限流。"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.errors import ApiError
from coreman.core.db.models import AdminSession, LoginAttempt, User

SESSION_COOKIE = "coreman_session"
CSRF_COOKIE = "coreman_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_MAX_AGE = 7 * 24 * 3600
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _db_ip(ip: str) -> str | None:
    """admin_sessions.ip / audit_logs.ip 是 INET 列；伪造的 X-Forwarded-For 等非法值直接
    写入会导致 INSERT 报错，这里校验失败时存 NULL 而不是让请求 500。"""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="coreman.session")


def sign_session_id(secret: str, session_id: uuid.UUID) -> str:
    return str(_serializer(secret).dumps(str(session_id)))


def unsign_session_id(secret: str, token: str, max_age: int = SESSION_MAX_AGE) -> uuid.UUID | None:
    try:
        raw = _serializer(secret).loads(token, max_age=max_age)
        return uuid.UUID(str(raw))
    except (BadSignature, SignatureExpired, ValueError):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def refresh_session_cookie(
    response: Response, *, secret: str, session_id: uuid.UUID, secure: bool
) -> None:
    """续期：只重签 session cookie（新时间戳 + 新 Max-Age），CSRF cookie 保持不变。"""
    response.set_cookie(
        SESSION_COOKIE,
        sign_session_id(secret, session_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def set_login_cookies(
    response: Response, *, secret: str, session_id: uuid.UUID, secure: bool
) -> str:
    refresh_session_cookie(response, secret=secret, session_id=session_id, secure=secure)
    csrf = new_csrf_token()
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_MAX_AGE,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return csrf


def clear_login_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


async def create_admin_session(
    session: AsyncSession, *, user: User, auth_method: str, ip: str | None, user_agent: str
) -> AdminSession:
    """建 admin_sessions 行并把 user.last_login_at 打到现在；只 flush，commit 交给调用方。

    bootstrap 登录与企微登录共用，避免重复拼装同一张表的写入逻辑。
    """
    now = datetime.now(UTC)
    row = AdminSession(
        user_id=user.id,
        auth_method=auth_method,
        ip=ip,
        user_agent=user_agent[:512],
        expires_at=now + timedelta(seconds=SESSION_MAX_AGE),
        last_seen_at=now,
    )
    user.last_login_at = now
    session.add(row)
    await session.flush()
    return row


async def verify_csrf(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    if request.cookies.get("bot_token"):
        # 必须先完成验签和当前用户状态校验，不能仅凭 cookie 存在就豁免 CSRF。
        from coreman.api.bot_auth import token_user

        try:
            async with request.app.state.session_factory() as session:
                await token_user(request, session)
            return
        except ApiError:
            # 令牌认不下来（多半是过期的残留 cookie）：不豁免，改走下面标准的 CSRF 校验。
            # 那条路更严，所以回落不会放宽任何限制；直接 401 反而会把正常登录的人挡在外面。
            pass
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise ApiError(403, 403, "CSRF 校验失败")


class DbLoginLimiter:
    """登录失败限流（5 次/分钟），记录在 login_attempts，api 多副本共享。

    只记失败：成功登录不计数，否则一个人正常登录几次就会把自己锁死。
    非法 IP（无法入 INET 列）统一记为 0.0.0.0，仍然计数。
    """

    def __init__(self, session: AsyncSession, max_hits: int = 5, window_seconds: int = 60) -> None:
        self._session, self._max, self._window = session, max_hits, window_seconds

    @staticmethod
    def _norm(ip: str) -> str:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return "0.0.0.0"
        return ip

    async def is_blocked(self, ip: str) -> bool:
        """窗口内的失败次数是否已达上限；只读，不写任何记录。"""
        since = datetime.now(UTC) - timedelta(seconds=self._window)
        hits = (
            await self._session.execute(
                select(func.count())
                .select_from(LoginAttempt)
                .where(LoginAttempt.ip == self._norm(ip), LoginAttempt.attempted_at > since)
            )
        ).scalar_one()
        return bool(hits >= self._max)

    async def record_failure(self, ip: str) -> None:
        """记一次失败，并顺带清掉 1 天前的旧记录。

        自行 commit：调用方紧接着就要抛 401，会话回滚后计数不能丢。
        """
        now = datetime.now(UTC)
        self._session.add(LoginAttempt(ip=self._norm(ip), attempted_at=now))
        await self._session.execute(
            delete(LoginAttempt).where(LoginAttempt.attempted_at < now - timedelta(days=1))
        )
        await self._session.commit()
