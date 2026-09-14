"""飞书登录（spec §10.1）：官方网页授权；state 单次使用，code 防重放。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

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
from coreman.core.platforms.feishu import FeishuClient, FeishuError, oauth_url

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
STATE_TTL = timedelta(minutes=10)
# state 与浏览器的绑定：start 下发明文、nonce 里只存 sha256（spec §10.1 登录 CSRF 防护）
BIND_COOKIE = "coreman_feishu_oauth"
BIND_PATH = "/api/auth/feishu"
_SAFE_PATH = re.compile(r"^/(?![/\\])")


def _fail(key: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={key}", status_code=302)


def _safe_redirect(value: str | None) -> str:
    """只接受站内路径：单个 / 开头，且第二个字符不是 / 或 \\（浏览器会把 \\ 归一化成 /）。"""
    return value if value and _SAFE_PATH.match(value) else "/"


async def _login_app(session: AsyncSession) -> PlatformApp | None:
    """取唯一一个启用且具备 login 能力的飞书应用；不存在则返回 None（由调用方决定失败方式）。"""
    stmt = select(PlatformApp).where(
        PlatformApp.platform == "feishu",
        PlatformApp.enabled.is_(True),
        PlatformApp.capabilities.any("login"),  # type: ignore[arg-type]
    )
    return (await session.execute(stmt.order_by(PlatformApp.created_at))).scalars().first()


@router.get("/feishu/start")
async def feishu_start(
    request: Request,
    redirect: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    app = await _login_app(session)
    if app is None:
        return _fail("feishu_not_configured")
    state = secrets.token_urlsafe(24)
    bind = secrets.token_urlsafe(24)
    session.add(
        AuthNonce(
            kind="feishu_state",
            value=state,
            payload={
                "redirect": _safe_redirect(redirect),
                "app_id": str(app.id),
                "app_version": app.version,
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
    callback = f"{settings.public_base_url}/api/auth/feishu/callback"
    url = oauth_url(app.app_id or "", callback, state)
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


@router.get("/feishu/callback")
async def feishu_callback(
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
    nonce = (
        (
            await session.execute(
                delete(AuthNonce)
                .where(AuthNonce.kind == "feishu_state", AuthNonce.value == state)
                .returning(AuthNonce)
            )
        ).scalar_one_or_none()
        if state
        else None
    )
    await session.commit()
    if nonce is None or datetime.now(UTC) - nonce.created_at > STATE_TTL:
        return _fail("invalid_state")
    payload = dict(nonce.payload)
    # 2) state 必须绑定发起登录的那个浏览器：否则攻击者把自己流程的 code+state 交给受害者，
    # 受害者会静默登录成攻击者账号（登录 CSRF）。绑定校验放在删除 nonce 之后，state 仍单次使用。
    bind = request.cookies.get(BIND_COOKIE)
    expected = str(payload.get("bind") or "")
    if not bind or not secrets.compare_digest(hashlib.sha256(bind.encode()).hexdigest(), expected):
        log.warning("feishu_login_state_unbound", ip=ip)
        return _fail("invalid_state")
    app = await _login_app(session)
    if app is None:
        return _fail("feishu_not_configured")
    if str(app.id) != payload.get("app_id") or app.version != payload.get("app_version"):
        return _fail("invalid_state")
    if not code or len(code) > 2048:
        return _fail("feishu_error")
    client = FeishuClient(app.app_id or "", decrypt_secret(request.app.state.cipher, app))
    try:
        info = await client.user_info_by_code(code)
    except FeishuError as exc:
        log.warning("feishu_login_failed", errcode=exc.code, ip=ip)
        return _fail("feishu_error")
    finally:
        await client.aclose()
    if app.corp_id and info.get("tenant_key") != app.corp_id:
        return _fail("not_member")
    userid = info.get("user_id")
    if not userid:
        # 非成员（只有 openid）：飞书应用不可见此人身份，不是「未绑定」，单独给一个 key。
        return _fail("not_member")
    ident = (
        await session.execute(
            select(UserIdentity)
            .options(selectinload(UserIdentity.user))
            .where(UserIdentity.platform == "feishu", UserIdentity.platform_user_id == str(userid))
        )
    ).scalar_one_or_none()
    if ident is None:
        log.warning("feishu_login_unknown_user", ip=ip)
        return _fail("user_not_found")
    user = ident.user
    if user.status != "active":
        return _fail("user_disabled")
    # 3) code 防重放：只在确认这次登录会成功之后才「消费」code——之前几步的失败（未绑定、
    # 已停用、非成员）都不应该烧掉它，否则用户改完绑定/启用状态后没法马上用同一个 code 重登。
    # auth_nonces 复合主键天然去重，flush 时撞主键即视为重放。
    # 已用 code 永久拒绝（飞书侧 code 本身 5 分钟内单次有效）；nonce 表每日清理。
    try:
        session.add(AuthNonce(kind="feishu_code", value=hashlib.sha256(code.encode()).hexdigest()))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return _fail("code_replayed")
    method = "feishu_oauth"
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
        action="auth.feishu_login",
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
    log.info("feishu_login_ok", user=actor_login, method=method, ip=ip)
    return response
