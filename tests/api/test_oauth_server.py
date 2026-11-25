"""CoreMan as an OAuth authorization server: registration, consent, code + PKCE, rotation,
revocation, scopes, and Bearer access to the admin API and the catalog MCP."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers.systems_catalog import PUBLIC_PATH
from coreman.core.db.models import User
from tests.api.conftest import login_as

REDIRECT = "http://127.0.0.1:53682/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


async def register(client: httpx.AsyncClient, **extra: Any) -> str:
    body = {"client_name": "Test Agent", "redirect_uris": [REDIRECT], **extra}
    r = await client.post("/api/oauth/register", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["client_id"])


async def authorize(
    client: httpx.AsyncClient,
    client_id: str,
    *,
    scope: str = "mcp",
    redirect_uri: str = REDIRECT,
    decision: str = "allow",
) -> tuple[dict[str, list[str]], str]:
    """登录状态下走完确认页，返回回调收到的参数和 verifier。"""
    verifier, challenge = pkce()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": scope,
    }
    page = await client.get("/api/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    assert "Test Agent" in page.text
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert ticket is not None
    r = await client.post(
        "/api/oauth/authorize", data={"ticket": ticket.group(1), "decision": decision}
    )
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith(redirect_uri + "?")
    return parse_qs(urlsplit(location).query), verifier


async def token(client: httpx.AsyncClient, **form: str) -> httpx.Response:
    return await client.post("/api/oauth/token", data=form)


async def tokens(
    client: httpx.AsyncClient, db_session: AsyncSession, *, scope: str = "mcp"
) -> tuple[dict[str, Any], str, User]:
    user = await login_as(client, db_session, display_name="张三")
    client_id = await register(client)
    query, verifier = await authorize(client, client_id, scope=scope)
    assert query["state"] == ["xyz"]
    assert query["iss"] == ["http://testserver"]
    r = await token(
        client,
        grant_type="authorization_code",
        code=query["code"][0],
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    )
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json(), client_id, user


def bearer_client(app: Any, access: str) -> httpx.AsyncClient:
    """不带任何 cookie 的客户端：只靠 Bearer 认人。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {access}"},
    )


async def test_metadata(client: httpx.AsyncClient) -> None:
    server = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert server["issuer"] == "http://testserver"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint_auth_methods_supported"] == ["none"]
    mcp = (await client.get(f"/.well-known/oauth-protected-resource{PUBLIC_PATH}")).json()
    assert mcp["resource"] == f"http://testserver{PUBLIC_PATH}"
    assert mcp["authorization_servers"] == ["http://testserver"]
    assert mcp["scopes_supported"] == ["mcp"]
    root = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert root["scopes_supported"] == ["api"]


@pytest.mark.parametrize(
    "body",
    [
        {"redirect_uris": ["https://evil.example/cb"]},
        {"redirect_uris": ["http://192.0.2.1:8000/cb"]},
        {"redirect_uris": ["http://127.0.0.1.evil.example/cb"]},
        {"redirect_uris": ["http://user@127.0.0.1/cb"]},
        {"redirect_uris": ["http://127.0.0.1/cb#frag"]},
        {"redirect_uris": []},
        {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
        {"redirect_uris": [REDIRECT], "grant_types": ["client_credentials"]},
    ],
)
async def test_register_only_public_loopback_clients(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    r = await client.post("/api/oauth/register", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


async def test_register_accepts_any_loopback_form(client: httpx.AsyncClient) -> None:
    for uri in ("http://localhost:8080/cb", "http://[::1]:9000/cb", "http://127.0.0.1/cb"):
        r = await client.post("/api/oauth/register", json={"redirect_uris": [uri]})
        assert r.status_code == 201, r.text
    browser = await client.post(
        "/api/oauth/register",
        json={"redirect_uris": [REDIRECT]},
        headers={"Origin": "https://evil.example"},
    )
    assert browser.status_code == 400


async def test_authorize_sends_anonymous_browser_to_login(client: httpx.AsyncClient) -> None:
    client_id = await register(client)
    _, challenge = pkce()
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 302
    location = urlsplit(r.headers["location"])
    assert location.path == "/login"
    target = parse_qs(location.query)["redirect"][0]
    assert target.startswith("/api/oauth/authorize?client_id=" + client_id)


async def test_authorize_never_redirects_to_unregistered_uri(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    client_id = await register(client)
    _, challenge = pkce()
    for redirect_uri in ("http://127.0.0.1:1/other", "https://evil.example/callback"):
        r = await client.get(
            "/api/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert r.status_code == 400
        assert "location" not in r.headers
    # 回环地址换端口按 RFC 8252 照样认。
    query, _ = await authorize(client, client_id, redirect_uri="http://127.0.0.1:40000/callback")
    assert "code" in query


async def test_authorize_requires_pkce(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session)
    client_id = await register(client)
    r = await client.get(
        "/api/oauth/authorize",
        params={"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code"},
    )
    assert r.status_code == 302
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["error"] == ["invalid_request"]


async def test_deny_returns_access_denied(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    client_id = await register(client)
    query, _ = await authorize(client, client_id, decision="deny")
    assert query["error"] == ["access_denied"]
    assert "code" not in query


async def test_consent_ticket_is_single_use_and_bound_to_login(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    client_id = await register(client)
    _, challenge = pkce()
    page = await client.get(
        "/api/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    match = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert match is not None
    ticket = match.group(1)
    # 换一个人登录：别人的确认凭证不能用。
    await login_as(client, db_session)
    other = await client.post("/api/oauth/authorize", data={"ticket": ticket, "decision": "allow"})
    assert other.status_code == 400
    # 凭证已经作废，原来的人也不能再用。
    again = await client.post("/api/oauth/authorize", data={"ticket": ticket, "decision": "allow"})
    assert again.status_code == 400


async def test_code_exchange_checks_pkce_and_is_single_use(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    client_id = await register(client)
    query, verifier = await authorize(client, client_id)
    code = query["code"][0]
    wrong = await token(
        client,
        grant_type="authorization_code",
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_verifier=pkce()[0],
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    # 校验失败也烧掉了授权码。
    replay = await token(
        client,
        grant_type="authorization_code",
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    )
    assert replay.status_code == 400


async def test_bearer_api_scope_reaches_admin_api_without_csrf(
    app: Any, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued, _, user = await tokens(client, db_session, scope="api")
    assert issued["scope"] == "api"
    async with bearer_client(app, issued["access_token"]) as api:
        me = await api.get("/api/admin/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["data"]["id"] == str(user.id)
        # 写请求不需要 CSRF 头。
        assert (await api.post("/api/admin/auth/logout")).status_code == 200
        # 本人不在场：只对管理台登录开放的接口拒绝。
        assert (await api.get("/api/me/api-tokens")).status_code == 403
        browser = await api.get("/api/admin/auth/me", headers={"Origin": "https://evil.example"})
        assert browser.status_code == 403


async def test_bearer_never_falls_back_to_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    assert (await client.get("/api/admin/auth/me")).status_code == 200
    r = await client.get("/api/admin/auth/me", headers={"Authorization": "Bearer cmo_bogus"})
    assert r.status_code == 401
    # Basic 之类浏览器会自动带上的方案不当令牌，照旧按 cookie 认人。
    basic = await client.get("/api/admin/auth/me", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert basic.status_code == 200


async def test_scopes_do_not_cross(
    app: Any, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued, _, _ = await tokens(client, db_session, scope="mcp")
    async with bearer_client(app, issued["access_token"]) as mcp:
        assert (await mcp.get("/api/admin/auth/me")).status_code == 403
        listed = await mcp.post(
            PUBLIC_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert listed.status_code == 200, listed.text
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert {"systems_browse", "systems_search", "systems_describe"} <= names
    issued_api, _, _ = await tokens(client, db_session, scope="api")
    async with bearer_client(app, issued_api["access_token"]) as api:
        r = await api.post(PUBLIC_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 403
        assert 'error="insufficient_scope"' in r.headers["WWW-Authenticate"]


async def test_mcp_rejects_bad_tokens_with_challenge(client: httpx.AsyncClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for value in ("Bearer cmo_bogus", "Bearer other", "Basic Zm9vOmJhcg=="):
        r = await client.post(PUBLIC_PATH, json=body, headers={"Authorization": value})
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["WWW-Authenticate"]


async def test_refresh_rotates_and_reuse_revokes_grant(
    app: Any, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued, client_id, _ = await tokens(client, db_session, scope="api")
    first = issued["refresh_token"]
    r = await token(client, grant_type="refresh_token", refresh_token=first, client_id=client_id)
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first
    widen = await token(
        client,
        grant_type="refresh_token",
        refresh_token=second["refresh_token"],
        client_id=client_id,
        scope="api mcp",
    )
    assert widen.status_code == 400
    assert widen.json()["error"] == "invalid_scope"
    # 旧的一代再出现：整次授权吊销，新换出来的也失效。
    reused = await token(
        client, grant_type="refresh_token", refresh_token=first, client_id=client_id
    )
    assert reused.status_code == 400
    async with bearer_client(app, second["access_token"]) as api:
        assert (await api.get("/api/admin/auth/me")).status_code == 401
    latest = await token(
        client,
        grant_type="refresh_token",
        refresh_token=second["refresh_token"],
        client_id=client_id,
    )
    assert latest.status_code == 400


async def test_revoke_and_disabled_member(
    app: Any, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued, client_id, _ = await tokens(client, db_session, scope="api")
    assert (
        await client.post("/api/oauth/revoke", data={"token": issued["access_token"]})
    ).status_code == 200
    async with bearer_client(app, issued["access_token"]) as api:
        assert (await api.get("/api/admin/auth/me")).status_code == 401
    refreshed = await token(
        client,
        grant_type="refresh_token",
        refresh_token=issued["refresh_token"],
        client_id=client_id,
    )
    assert refreshed.status_code == 400

    issued2, _, user = await tokens(client, db_session, scope="api")
    user.status = "disabled"
    db_session.add(user)
    await db_session.commit()
    async with bearer_client(app, issued2["access_token"]) as api:
        assert (await api.get("/api/admin/auth/me")).status_code == 401


async def test_member_lists_and_revokes_own_grants(
    app: Any, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued, _, _ = await tokens(client, db_session, scope="mcp")
    grants = (await client.get("/api/me/oauth-grants")).json()["data"]
    assert [g["client_name"] for g in grants] == ["Test Agent"]
    assert grants[0]["scope"] == ["mcp"]
    r = await client.delete(f"/api/me/oauth-grants/{grants[0]['id']}")
    assert r.status_code == 200, r.text
    assert (await client.get("/api/me/oauth-grants")).json()["data"] == []
    async with bearer_client(app, issued["access_token"]) as mcp:
        r = await mcp.post(PUBLIC_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
    # 别人看不到、也撤不了。
    await login_as(client, db_session)
    assert (await client.get("/api/me/oauth-grants")).json()["data"] == []
    other = await client.delete(f"/api/me/oauth-grants/{grants[0]['id']}")
    assert other.status_code == 404
