"""CoreMan 的 OAuth 授权服务器端点：元数据、动态注册、授权确认页、换令牌、吊销，以及成员管理
自己的授权。规则在 coreman.core.oauth.server。"""

from __future__ import annotations

import html
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.routers.systems_catalog import PUBLIC_PATH as MCP_PATH
from coreman.api.security import _db_ip, verify_csrf
from coreman.core.audit import record_audit
from coreman.core.db.models import AdminSession, OAuthClient, OAuthGrant, User
from coreman.core.oauth import server
from coreman.core.oauth.server import OAuthError
from coreman.core.timeutils import utcnow

router = APIRouter(tags=["oauth"])
AUTHORIZE_PATH = "/api/oauth/authorize"
TOKEN_PATH = "/api/oauth/token"
REGISTER_PATH = "/api/oauth/register"
REVOKE_PATH = "/api/oauth/revoke"
MAX_FORM = 16_384
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def base_url(request: Request) -> str:
    return str(request.app.state.settings.public_base_url).rstrip("/")


def resource_metadata_url(request: Request, path: str = MCP_PATH) -> str:
    return f"{base_url(request)}/.well-known/oauth-protected-resource{path}"


def resources(request: Request) -> dict[str, str]:
    """这台服务器上的受保护资源 → 用得上它的 scope（RFC 8707 的 `resource` 只认这些）。"""
    base = base_url(request)
    return {base + MCP_PATH: "mcp", base: "api"}


def _resource(request: Request, path: str) -> dict[str, Any]:
    """受保护资源元数据（RFC 9728）：MCP 端点只给 mcp scope，其余（管理接口）给 api。"""
    base = base_url(request)
    mcp = path.rstrip("/") == MCP_PATH
    return {
        "resource": base + (MCP_PATH if mcp else ""),
        "authorization_servers": [base],
        "scopes_supported": ["mcp"] if mcp else ["api"],
        "bearer_methods_supported": ["header"],
        "resource_name": "CoreMan",
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_root(request: Request) -> JSONResponse:
    return JSONResponse(_resource(request, ""))


@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource(path: str, request: Request) -> JSONResponse:
    return JSONResponse(_resource(request, "/" + path))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request) -> JSONResponse:
    base = base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": base + AUTHORIZE_PATH,
            "token_endpoint": base + TOKEN_PATH,
            "registration_endpoint": base + REGISTER_PATH,
            "revocation_endpoint": base + REVOKE_PATH,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": sorted(server.SCOPES),
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": True,
        }
    )


def _error(exc: OAuthError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status, headers=NO_STORE)


@router.post(REGISTER_PATH)
async def register(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    if "origin" in request.headers:
        return _error(OAuthError("invalid_request", "不支持从浏览器页面注册"))
    try:
        metadata = await request.json()
    except ValueError:
        return _error(OAuthError("invalid_client_metadata", "注册信息必须是 JSON"))
    try:
        client = await server.register(session, metadata, _db_ip(client_ip(request)))
    except OAuthError as exc:
        return _error(exc)
    await session.commit()
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
        headers=NO_STORE,
    )


async def _login(request: Request, session: AsyncSession) -> tuple[User, AdminSession] | None:
    """浏览器里本人的管理台登录；没有登录、或不是本人在场的会话（管理命令）都算没有。"""
    try:
        user = await current_user(request, Response(), session)
    except ApiError:
        return None
    login = getattr(request.state, "admin_session", None)
    if login is None or login.auth_method == "cli":
        return None
    return user, login


_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title} · CoreMan</title><style>
:root{{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1f2329;--muted:#646a73;
--line:#e4e6eb;--primary:#3370ff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--card:#1f2228;--fg:#e8eaed;
--muted:#9aa0a6;--line:#2e3138;--primary:#5b8cff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif}}
main{{max-width:440px;margin:10vh auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}}
h1{{font-size:20px;margin:0 0 12px}}p,li{{color:var(--muted)}}b{{color:var(--fg)}}
ul{{padding-left:20px}}.row{{display:flex;gap:12px;margin-top:20px}}
button{{flex:1;padding:10px;border-radius:8px;border:1px solid var(--line);font-size:15px;
cursor:pointer;background:var(--card);color:var(--fg)}}
button.primary{{background:var(--primary);border-color:var(--primary);color:#fff}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), body=body),
        status_code=status,
        headers={
            **NO_STORE,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self' http://127.0.0.1:* http://localhost:* http://[::1]:*; "
            "frame-ancestors 'none'",
            "X-Frame-Options": "DENY",
        },
    )


def _error_page(message: str) -> HTMLResponse:
    return _page(
        "无法授权",
        f"<h1>无法授权</h1><p>{html.escape(message)}</p>",
        status=400,
    )


def _back(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    joiner = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + joiner + urlencode(params), status_code=302)


@router.get(AUTHORIZE_PATH)
async def authorize(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    params = {k: v for k, v in request.query_params.items()}
    found = await _login(request, session)
    login_page = RedirectResponse(
        "/login?" + urlencode({"redirect": request.url.path + "?" + request.url.query}),
        status_code=302,
    )
    client_id = params.get("client_id") or ""
    document_client = server.metadata_client_id(client_id)
    try:
        if document_client:
            # 元数据文档要平台出站去取：先登录，只有成员本人发起的授权才会触发。
            if found is None:
                return login_page
            await server.sync_metadata_client(
                session,
                client_id,
                await server.fetch_client_document(client_id),
                _db_ip(client_ip(request)),
            )
        req = await server.check_authorize(
            session, params, request.query_params.getlist("resource")
        )
    except OAuthError as exc:
        await session.rollback()
        return _error_page(exc.description)
    try:
        server.check_redirectable(req, params, resources(request))
    except OAuthError as exc:
        return _back(
            req.redirect_uri,
            {"error": exc.error, "error_description": exc.description, "iss": base_url(request)}
            | ({"state": req.state} if req.state else {}),
        )
    if found is None:
        return login_page
    user, login = found
    ticket = await server.start_consent(session, req, user, login.id)
    await session.commit()
    port = req.redirect_uri.split("//", 1)[1].split("/", 1)[0]
    scopes = "".join(
        f"<li>{html.escape(server.SCOPES[name])}</li>"
        for name in server.parse_scope(req.scope).split()
    )
    body = (
        "<h1>授权本机应用</h1>"
        f"<p>客户端自报名称：<b>{html.escape(req.client.client_name)}</b></p>"
        + (
            f"<p>客户端标识：<b>{html.escape(req.client.client_id)}</b></p>"
            if document_client
            else ""
        )
        + f"<p>授权后令牌交给本机的 <b>{html.escape(port)}</b>。"
        "只在你刚刚亲自从本机应用发起登录时才允许。</p>"
        f"<p>以 <b>{html.escape(user.display_name)}</b> 的身份：</p><ul>{scopes}</ul>"
        f'<form method="post" action="{AUTHORIZE_PATH}">'
        f'<input type="hidden" name="ticket" value="{html.escape(ticket)}">'
        '<div class="row"><button name="decision" value="deny">拒绝</button>'
        '<button class="primary" name="decision" value="allow">允许</button></div></form>'
    )
    return _page("授权本机应用", body)


async def _form(request: Request) -> dict[str, str]:
    raw = await mcp_rpc.read_body(request, MAX_FORM)
    try:
        pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        return {}
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:
            # 同名参数出现两次按 RFC 6749 §3.1 拒绝。
            return {"__duplicate__": key}
        form[key] = value
    return form


@router.post(AUTHORIZE_PATH)
async def authorize_decide(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    form = await _form(request)
    found = await _login(request, session)
    if found is None:
        return _error_page("登录已失效，请回到客户端重新发起")
    user, login = found
    try:
        consent = await server.take_consent(session, form.get("ticket", ""), user, login.id)
    except OAuthError as exc:
        await session.commit()
        return _error_page(exc.description)
    state = {"state": consent["state"]} if consent.get("state") else {}
    iss = {"iss": base_url(request)}
    if form.get("decision") != "allow":
        await session.commit()
        return _back(
            consent["redirect_uri"],
            {"error": "access_denied", "error_description": "用户拒绝了授权"} | state | iss,
        )
    code = await server.issue_code(session, consent)
    await record_audit(
        session,
        action="oauth.authorize",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="oauth_client",
        target_id=str(consent["client_id"]),
        diff={"scope": consent["scope"]},
        ip=_db_ip(client_ip(request)),
    )
    await session.commit()
    return _back(consent["redirect_uri"], {"code": code} | state | iss)


@router.post(TOKEN_PATH)
async def token(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    if "origin" in request.headers:
        return _error(OAuthError("invalid_request", "不支持从浏览器页面换令牌"))
    form = await _form(request)
    if "__duplicate__" in form:
        return _error(OAuthError("invalid_request", "参数重复"))
    client_id = form.get("client_id", "")
    if await session.get(OAuthClient, client_id) is None:
        return _error(OAuthError("invalid_client", "客户端未注册", status=401))
    grant_type = form.get("grant_type")
    try:
        if grant_type == "authorization_code":
            issued, grant = await server.exchange_code(
                session,
                code=form.get("code", ""),
                client_id=client_id,
                redirect_uri=form.get("redirect_uri", ""),
                verifier=form.get("code_verifier", ""),
                resource=form.get("resource"),
                known=resources(request),
            )
        elif grant_type == "refresh_token":
            issued, grant = await server.refresh_grant(
                session,
                refresh_token=form.get("refresh_token", ""),
                client_id=client_id,
                scope=form.get("scope"),
                resource=form.get("resource"),
                known=resources(request),
            )
        else:
            raise OAuthError("unsupported_grant_type", "只支持 authorization_code 与 refresh_token")
    except OAuthError as exc:
        await session.rollback()
        return _error(exc)
    await session.commit()
    return JSONResponse(issued.body(), headers=NO_STORE)


@router.post(REVOKE_PATH)
async def revoke(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if "origin" in request.headers:
        return _error(OAuthError("invalid_request", "不支持从浏览器页面吊销"))
    form = await _form(request)
    await server.revoke(session, form.get("token", ""), form.get("client_id") or None)
    await session.commit()
    return Response(status_code=200, headers=NO_STORE)


# ---- 成员管理自己的授权（管理台「我的 API 令牌」页） ----


def _grant_out(grant: OAuthGrant, client: OAuthClient) -> dict[str, Any]:
    return {
        "id": str(grant.id),
        "client_name": client.client_name,
        "scope": grant.scope.split(),
        "created_at": grant.created_at.isoformat(),
        "last_used_at": grant.last_used_at.isoformat() if grant.last_used_at else None,
        "expires_at": grant.refresh_expires_at.isoformat(),
    }


@router.get("/api/me/oauth-grants")
async def my_grants(
    response: Response,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers.update(mcp_rpc.NO_STORE)
    rows = await session.execute(
        select(OAuthGrant, OAuthClient)
        .join(OAuthClient, OAuthClient.client_id == OAuthGrant.client_id)
        .where(
            OAuthGrant.user_id == actor.id,
            OAuthGrant.revoked_at.is_(None),
            OAuthGrant.refresh_expires_at > utcnow(),
        )
        .order_by(OAuthGrant.created_at.desc())
    )
    return {"code": 0, "data": [_grant_out(g, c) for g, c in rows.all()]}


@router.delete("/api/me/oauth-grants/{grant_id}", dependencies=[Depends(verify_csrf)])
async def revoke_my_grant(
    grant_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    grant = await session.get(OAuthGrant, grant_id, with_for_update=True)
    if grant is None or grant.user_id != actor.id:
        raise not_found("授权不存在")
    if grant.revoked_at is None:
        await server.revoke_grant(session, grant)
        await record_audit(
            session,
            action="oauth.revoke",
            actor_id=actor.id,
            actor_login=actor.login_name,
            target_type="oauth_grant",
            target_id=str(grant.id),
            ip=_db_ip(client_ip(request)),
        )
    await session.commit()
    return {"code": 0, "data": None}
