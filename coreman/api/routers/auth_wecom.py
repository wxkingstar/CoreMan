"""企业微信登录：扫码 wwlogin 与企微内 oauth2；state 单次使用，code 防重放。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coreman.api.deps import client_ip, get_session
from coreman.api.routers.platform_apps import decrypt_secret
from coreman.api.security import _db_ip, create_admin_session, set_login_cookies
from coreman.core.audit import record_audit
from coreman.core.db.models import AuthNonce, PlatformApp, UserIdentity
from coreman.core.logging import get_logger
from coreman.core.platforms.wecom import WeComClient, WeComError, oauth_url, qr_login_url

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
STATE_TTL = timedelta(minutes=10)
# state 与浏览器的绑定：start 下发明文、nonce 里只存 sha256（登录 CSRF 防护）
BIND_COOKIE = "coreman_oauth"
BIND_PATH = "/api/auth/wecom"
Mode = Literal["qr", "oauth"]
_SAFE_PATH = re.compile(r"^/(?![/\\])")


def _fail(key: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={key}", status_code=302)


def _safe_redirect(value: str | None) -> str:
    """只接受站内路径：单个 / 开头，且第二个字符不是 / 或 \\（浏览器会把 \\ 归一化成 /）。"""
    return value if value and _SAFE_PATH.match(value) else "/"


async def _login_app(session: AsyncSession) -> PlatformApp | None:
    """取唯一一个启用且具备 login 能力的企微应用；不存在则返回 None（由调用方决定失败方式）。"""
    stmt = select(PlatformApp).where(
        PlatformApp.platform == "wecom",
        PlatformApp.enabled.is_(True),
        PlatformApp.capabilities.any("login"),  # type: ignore[arg-type]
    )
    return (await session.execute(stmt.order_by(PlatformApp.created_at))).scalars().first()


@router.get("/providers")
async def providers(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from coreman.api.routers.auth_feishu import _login_app as _feishu_login_app

    return {
        "code": 0,
        "data": {
            "wecom": await _login_app(session) is not None,
            "feishu": await _feishu_login_app(session) is not None,
        },
    }


@router.get("/wecom/start")
async def wecom_start(
    request: Request,
    mode: Mode = "qr",
    redirect: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    app = await _login_app(session)
    if app is None:
        return _fail("wecom_not_configured")
    state = secrets.token_urlsafe(24)
    bind = secrets.token_urlsafe(24)
    session.add(
        AuthNonce(
            kind="wecom_state",
            value=state,
            payload={
                "redirect": _safe_redirect(redirect),
                "mode": mode,
                "bind": hashlib.sha256(bind.encode()).hexdigest(),
            },
        )
    )
    # 顺带清掉过期一天以上的 nonce（state 与 code 共用同一张表），callback 里不做这个清理。
    await session.execute(
        delete(AuthNonce).where(AuthNonce.created_at < datetime.now(UTC) - timedelta(days=1))
    )
    await session.commit()
    settings = request.app.state.settings
    callback = f"{settings.public_base_url}/api/auth/wecom/callback"
    build = qr_login_url if mode == "qr" else oauth_url
    url = build(app.corp_id or "", app.app_id or "", callback, state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        BIND_COOKIE,
        bind,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
        path=BIND_PATH,
    )
    return response


@router.get("/wecom/callback")
async def wecom_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """回调的统一出口：无论成功还是失败，都把一次性的绑定 cookie 清掉。"""
    response = await _handle_callback(request, code, state, session)
    response.delete_cookie(BIND_COOKIE, path=BIND_PATH)
    return response


async def _handle_callback(
    request: Request, code: str, state: str, session: AsyncSession
) -> RedirectResponse:
    ip = client_ip(request)
    # 1) state：单次使用 + 10 分钟 TTL。取到就立即删并 commit，任何后续失败都不会让它复活。
    nonce = await session.get(AuthNonce, {"kind": "wecom_state", "value": state}) if state else None
    if nonce is None or datetime.now(UTC) - nonce.created_at > STATE_TTL:
        return _fail("invalid_state")
    payload = dict(nonce.payload)
    await session.delete(nonce)
    await session.commit()
    # 2) state 必须绑定发起登录的那个浏览器：否则攻击者把自己流程的 code+state 交给受害者，
    # 受害者会静默登录成攻击者账号（登录 CSRF）。绑定校验放在删除 nonce 之后，state 仍单次使用。
    bind = request.cookies.get(BIND_COOKIE)
    expected = str(payload.get("bind") or "")
    if not bind or not secrets.compare_digest(hashlib.sha256(bind.encode()).hexdigest(), expected):
        log.warning("wecom_login_state_unbound", ip=ip)
        return _fail("invalid_state")
    app = await _login_app(session)
    if app is None:
        return _fail("wecom_not_configured")
    client = WeComClient(app.corp_id or "", decrypt_secret(request.app.state.cipher, app))
    try:
        info = await client.user_info_by_code(code)
    except WeComError as exc:
        log.warning("wecom_login_failed", errcode=exc.errcode, ip=ip)
        return _fail("wecom_error")
    finally:
        await client.aclose()
    userid = info.get("userid")
    if not userid:
        # 非成员（只有 openid）：企微应用不可见此人身份，不是「未绑定」，单独给一个 key。
        return _fail("not_member")
    ident = (
        await session.execute(
            select(UserIdentity)
            .options(selectinload(UserIdentity.user))
            .where(UserIdentity.platform == "wecom", UserIdentity.platform_user_id == str(userid))
        )
    ).scalar_one_or_none()
    if ident is None:
        log.warning("wecom_login_unknown_user", ip=ip)
        return _fail("user_not_found")
    user = ident.user
    if user.status != "active":
        return _fail("user_disabled")
    # 3) code 防重放：只在确认这次登录会成功之后才「消费」code——之前几步的失败（未绑定、
    # 已停用、非成员）都不应该烧掉它，否则用户改完绑定/启用状态后没法马上用同一个 code 重登。
    # auth_nonces 复合主键天然去重，flush 时撞主键即视为重放。
    # 已用 code 永久拒绝（企微侧 code 本身 5 分钟内单次有效）；nonce 表每日清理。
    try:
        session.add(AuthNonce(kind="wecom_code", value=code))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return _fail("code_replayed")
    method: str = "wecom_qr" if payload.get("mode") == "qr" else "wecom_oauth"
    actor_login = user.login_name or f"user:{user.id}"
    admin_session = await create_admin_session(
        session,
        user=user,
        auth_method=method,
        ip=_db_ip(ip),
        user_agent=request.headers.get("user-agent", ""),
    )
    await record_audit(
        session,
        action="auth.wecom_login",
        actor_id=user.id,
        actor_login=actor_login,
        target_type="user",
        target_id=str(user.id),
        ip=_db_ip(ip),
    )
    await session.commit()
    response = RedirectResponse(_safe_redirect(payload.get("redirect")), status_code=302)
    settings = request.app.state.settings
    set_login_cookies(
        response,
        secret=settings.session_secret,
        session_id=admin_session.id,
        secure=settings.public_base_url.startswith("https://"),
    )
    log.info("wecom_login_ok", user=actor_login, method=method, ip=ip)
    return response
