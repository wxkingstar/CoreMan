"""通用 OIDC 登录：授权码 + PKCE + nonce；state 单次使用并绑定发起登录的浏览器。

按 id_token 的 email 对应已同步进来的成员（不区分大小写），不自动建号。规则在 core.auth.oidc。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.routers.auth_feishu import _safe_redirect
from coreman.api.security import _db_ip, create_admin_session, set_login_cookies
from coreman.core.audit import record_audit
from coreman.core.auth import oidc
from coreman.core.db.models import AuthNonce, User
from coreman.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
STATE_KIND = "oidc_state"
STATE_TTL = timedelta(minutes=10)
BIND_COOKIE = "coreman_oidc_oauth"
BIND_PATH = "/api/auth/oidc"


def _fail(key: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={key}", status_code=302)


def _callback_url(request: Request) -> str:
    return f"{request.app.state.settings.public_base_url}{BIND_PATH}/callback"


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=oidc.TIMEOUT)


@router.get("/oidc/start")
async def oidc_start(
    request: Request, redirect: str | None = None, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    settings = request.app.state.settings
    if not settings.oidc_enabled:
        return _fail("oidc_not_configured")
    try:
        async with _http() as client:
            found = await oidc.provider(client, settings.oidc_issuer)
    except (oidc.OIDCError, httpx.HTTPError, ValueError) as exc:
        log.warning("oidc_discovery_failed", error=str(exc))
        return _fail("oidc_error")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    bind = request.cookies.get(BIND_COOKIE) or secrets.token_urlsafe(24)
    session.add(
        AuthNonce(
            kind=STATE_KIND,
            value=state,
            payload={
                "redirect": _safe_redirect(redirect),
                "bind": hashlib.sha256(bind.encode()).hexdigest(),
                "nonce": nonce,
                "verifier": verifier,
            },
        )
    )
    await session.execute(
        delete(AuthNonce).where(
            AuthNonce.kind == STATE_KIND,
            AuthNonce.created_at < datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.commit()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": _callback_url(request),
            "scope": settings.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
    )
    joiner = "&" if "?" in found.authorization_endpoint else "?"
    response = RedirectResponse(found.authorization_endpoint + joiner + query, status_code=302)
    response.set_cookie(
        BIND_COOKIE,
        bind,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
        path=BIND_PATH,
    )
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    response = await _handle_callback(request, code, state, error, session)
    response.delete_cookie(BIND_COOKIE, path=BIND_PATH)
    return response


async def _handle_callback(
    request: Request, code: str, state: str, error: str, session: AsyncSession
) -> RedirectResponse:
    settings = request.app.state.settings
    ip = client_ip(request)
    # state：单次使用 + 10 分钟；取到就删并提交，后面任何失败都不会让它复活。
    nonce_row = (
        (
            await session.execute(
                delete(AuthNonce)
                .where(AuthNonce.kind == STATE_KIND, AuthNonce.value == state)
                .returning(AuthNonce)
            )
        ).scalar_one_or_none()
        if state
        else None
    )
    await session.commit()
    if nonce_row is None or datetime.now(UTC) - nonce_row.created_at > STATE_TTL:
        return _fail("invalid_state")
    payload = dict(nonce_row.payload)
    # state 绑定发起登录的浏览器，防登录 CSRF（见 auth_feishu 同一处的说明）。
    bind = request.cookies.get(BIND_COOKIE)
    expected = str(payload.get("bind") or "")
    if not bind or not secrets.compare_digest(hashlib.sha256(bind.encode()).hexdigest(), expected):
        log.warning("oidc_login_state_unbound", ip=ip)
        return _fail("invalid_state")
    if not settings.oidc_enabled:
        return _fail("oidc_not_configured")
    if error or not code or len(code) > 4096:
        log.warning("oidc_login_denied", error=error[:100], ip=ip)
        return _fail("oidc_error")
    try:
        async with _http() as client:
            found = await oidc.provider(client, settings.oidc_issuer)
            token = await oidc.exchange(
                client,
                found,
                code=code,
                redirect_uri=_callback_url(request),
                verifier=str(payload["verifier"]),
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret.get_secret_value(),
            )
            claims = await oidc.verify(
                client,
                found,
                token,
                client_id=settings.oidc_client_id,
                nonce=str(payload["nonce"]),
            )
    except (oidc.OIDCError, httpx.HTTPError, ValueError) as exc:
        log.warning("oidc_login_failed", error=str(exc), ip=ip)
        return _fail("oidc_error")
    email = oidc.email_of(claims)
    if email is None:
        log.warning("oidc_login_no_email", ip=ip)
        return _fail("user_not_found")
    user = await session.scalar(
        select(User).where(func.lower(User.email) == email, User.source != "bootstrap")
    )
    if user is None:
        log.warning("oidc_login_unknown_user", ip=ip)
        return _fail("user_not_found")
    if user.status != "active":
        return _fail("user_disabled")
    actor_login = user.login_name or f"user:{user.id}"
    admin_session = await create_admin_session(
        session,
        user=user,
        auth_method="oidc",
        ip=_db_ip(ip),
        user_agent=request.headers.get("user-agent", ""),
    )
    await record_audit(
        session,
        action="auth.oidc_login",
        actor_id=user.id,
        actor_login=actor_login,
        target_type="user",
        target_id=str(user.id),
        ip=_db_ip(ip),
    )
    await session.commit()
    response = RedirectResponse(_safe_redirect(payload.get("redirect")), status_code=302)
    set_login_cookies(
        response,
        secret=settings.session_secret,
        session_id=admin_session.id,
        secure=settings.public_base_url.startswith("https://"),
    )
    log.info("oidc_login_ok", user=actor_login, ip=ip)
    return response
