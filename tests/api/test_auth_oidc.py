"""Generic OIDC sign-in against a fake identity provider: discovery, code + PKCE + nonce, id_token
verification, and mapping to an existing member by email."""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth import oidc
from coreman.core.db.models import AdminSession, User

ISSUER = "https://idp.example.test"
CLIENT_ID = "coreman"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    data: dict[str, Any] = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**data, "kid": kid, "alg": "ES256", "use": "sig"}


@pytest.fixture
def configured(app: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = app.state.settings
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", SecretStr("client-secret"))
    monkeypatch.setattr(settings, "oidc_display_name", "统一登录")
    oidc._providers.clear()
    oidc._keys.clear()
    yield
    oidc._providers.clear()
    oidc._keys.clear()


def id_token(nonce: str, **overrides: Any) -> str:
    key = overrides.pop("_key", KEY)
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "zhangsan",
        "email": "ZhangSan@Example.test",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "k1"})


class FakeIdP:
    def __init__(self, router: respx.MockRouter) -> None:
        self.token: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        router.get(f"{ISSUER}/.well-known/openid-configuration").respond(
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                "id_token_signing_alg_values_supported": ["ES256"],
            }
        )
        router.get(f"{ISSUER}/.well-known/jwks.json").respond(json={"keys": [jwk(KEY, "k1")]})
        router.post(f"{ISSUER}/oauth/token").mock(side_effect=self.exchange)

    def exchange(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.token)


async def start(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.get("/api/auth/oidc/start", params={"redirect": "/bots"})
    assert r.status_code == 302, r.text
    location = urlsplit(r.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/oauth/authorize"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def member(db_session: AsyncSession, **kw: Any) -> User:
    user = User(
        login_name="zhangsan",
        display_name="张三",
        email="zhangsan@example.test",
        source="sync",
        **kw,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_providers_show_oidc_only_when_configured(
    client: httpx.AsyncClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/api/auth/providers")).json()["data"]["oidc"] is None
    r = await client.get("/api/auth/oidc/start")
    assert r.headers["location"] == "/login?error=oidc_not_configured"
    monkeypatch.setattr(app.state.settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(app.state.settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(app.state.settings, "oidc_client_secret", SecretStr("s"))
    assert (await client.get("/api/auth/providers")).json()["data"]["oidc"] == "SSO"


async def test_signs_in_existing_member_by_email(
    client: httpx.AsyncClient, db_session: AsyncSession, configured: None
) -> None:
    user = await member(db_session)
    with respx.mock(assert_all_called=False) as router:
        idp = FakeIdP(router)
        params = await start(client)
        assert params["client_id"] == CLIENT_ID
        assert params["scope"] == "openid profile email"
        assert params["code_challenge_method"] == "S256"
        assert params["redirect_uri"] == "http://testserver/api/auth/oidc/callback"
        idp.token = {"id_token": id_token(params["nonce"]), "access_token": "x"}
        r = await client.get(
            "/api/auth/oidc/callback", params={"code": "c1", "state": params["state"]}
        )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/bots"
    sent = idp.requests[0]
    assert (
        sent.headers["authorization"]
        == "Basic " + base64.b64encode(b"coreman:client-secret").decode()
    )
    form = parse_qs(sent.content.decode())
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(form["code_verifier"][0].encode()).digest()
    ).rstrip(b"=")
    assert challenge.decode() == params["code_challenge"]
    row = await db_session.scalar(select(AdminSession).where(AdminSession.user_id == user.id))
    assert row is not None and row.auth_method == "oidc"
    me = await client.get("/api/admin/auth/me")
    assert me.json()["data"]["id"] == str(user.id)


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"nonce": "other"}, "oidc_error"),
        ({"aud": "someone-else"}, "oidc_error"),
        ({"iss": "https://evil.example.test"}, "oidc_error"),
        ({"exp": int(time.time()) - 3600}, "oidc_error"),
        ({"_key": OTHER_KEY}, "oidc_error"),
        ({"aud": [CLIENT_ID, "other"], "azp": "other"}, "oidc_error"),
        ({"email": "nobody@example.test"}, "user_not_found"),
        ({"email_verified": False}, "user_not_found"),
    ],
)
async def test_rejects_bad_id_tokens(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    configured: None,
    claims: dict[str, Any],
    expected: str,
) -> None:
    await member(db_session)
    with respx.mock(assert_all_called=False) as router:
        idp = FakeIdP(router)
        params = await start(client)
        nonce = claims.pop("nonce", params["nonce"])
        idp.token = {"id_token": id_token(nonce, **claims)}
        r = await client.get(
            "/api/auth/oidc/callback", params={"code": "c1", "state": params["state"]}
        )
    assert r.headers["location"] == f"/login?error={expected}"
    assert (await client.get("/api/admin/auth/me")).status_code == 401


async def test_rejects_disabled_member_unbound_and_replayed_state(
    client: httpx.AsyncClient, db_session: AsyncSession, configured: None
) -> None:
    await member(db_session, status="disabled")
    with respx.mock(assert_all_called=False) as router:
        idp = FakeIdP(router)
        params = await start(client)
        idp.token = {"id_token": id_token(params["nonce"])}
        disabled = await client.get(
            "/api/auth/oidc/callback", params={"code": "c1", "state": params["state"]}
        )
        assert disabled.headers["location"] == "/login?error=user_disabled"
        # state 已经用过。
        again = await client.get(
            "/api/auth/oidc/callback", params={"code": "c1", "state": params["state"]}
        )
        assert again.headers["location"] == "/login?error=invalid_state"
        # 别的浏览器（没有绑定 cookie）拿到 state 也不能用。
        params = await start(client)
        client.cookies.clear()
        unbound = await client.get(
            "/api/auth/oidc/callback", params={"code": "c1", "state": params["state"]}
        )
        assert unbound.headers["location"] == "/login?error=invalid_state"


async def test_rejects_discovery_with_wrong_issuer_or_symmetric_algorithms(
    client: httpx.AsyncClient, configured: None
) -> None:
    for document in (
        {"issuer": "https://evil.example.test"},
        {"id_token_signing_alg_values_supported": ["HS256", "none"]},
        {"token_endpoint": "http://idp.example.test/oauth/token"},
    ):
        oidc._providers.clear()
        with respx.mock(assert_all_called=False) as router:
            base = {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
            }
            router.get(f"{ISSUER}/.well-known/openid-configuration").respond(json=base | document)
            r = await client.get("/api/auth/oidc/start")
        assert r.headers["location"] == "/login?error=oidc_error"
