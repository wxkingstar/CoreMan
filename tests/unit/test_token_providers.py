"""Issuer configuration and the real HTTP adapter, with external I/O isolated by respx."""

import base64
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from coreman.core.config import Settings


def settings(providers: dict) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        PUBLIC_BASE_URL="https://coreman.example",
        MASTER_KEY=base64.b64encode(b"x" * 32).decode(),
        SESSION_SECRET="s" * 32,
        BUSINESS_TOKEN_PROVIDERS=providers,
    )


def config(**extra: object) -> dict:
    return {
        "token_url": "https://identity.example/oauth/agent-token",
        "client_id": "coreman",
        "client_secret": "private-service-secret",
        **extra,
    }


def test_provider_config_loads_without_exposing_credentials() -> None:
    s = settings({"company": config(max_token_ttl_seconds=28800)})
    assert s.business_token_providers["company"].max_token_ttl_seconds == 28800
    assert "private-service-secret" not in repr(s)
    assert "private-service-secret" not in s.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"token_url": "http://identity.example/token"},
        {"token_url": "https://identity.example/token?secret=x"},
        {"token_url": "https://u:p@identity.example/token"},
        {"token_url": "https://identity.example/token#fragment"},
        {"max_token_ttl_seconds": 0},
        {"max_token_ttl_seconds": -10},
        {"max_token_ttl_seconds": True},
        {"client_secret": ""},
        {"subject_field": "audience"},
        {"subject_field": "client_secret"},
    ],
)
def test_invalid_provider_config_fails_startup(changes: dict) -> None:
    with pytest.raises(ValueError) as exc:
        settings({"company": config(**changes)})
    assert "private-service-secret" not in str(exc.value)


def test_builtin_cannot_be_replaced_by_an_http_provider() -> None:
    with pytest.raises(ValueError):
        settings({"builtin": config()})


@pytest.mark.parametrize(
    ("task_timeout", "cap", "expected"),
    [
        (1800, 28800, 1800),
        (43200, 28800, 28800),
        (43200, None, 43200),
    ],
)
@respx.mock
async def test_http_issuer_uses_trusted_subject_and_caps_task_timeout(task_timeout, cap, expected):
    from coreman.core.auth.token_providers import HTTPTokenProvider, TokenRequest

    cfg = settings({"company": config(max_token_ttl_seconds=cap)}).business_token_providers[
        "company"
    ]
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "opaque-private-access-token",
                "token_type": "Bearer",
                "expires_in": 1200,
            },
        )

    respx.post(cfg.token_url).mock(side_effect=handler)
    token = await HTTPTokenProvider(cfg).issue(
        TokenRequest(subject="alice", audience="erp", ttl_seconds=task_timeout)
    )
    assert parse_qs(observed[0].content.decode()) == {
        "username": ["alice"],
        "audience": ["erp"],
        "expires_in": [str(expected)],
    }
    assert (
        observed[0].headers["Authorization"]
        == "Basic " + base64.b64encode(b"coreman:private-service-secret").decode()
    )
    assert token.value == "opaque-private-access-token" and token.expires_in == 1200
    assert token.auth_mode == "bearer" and "opaque-private-access-token" not in repr(token)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": "access_denied", "error_description": "PRIVATE BODY"}),
        httpx.Response(302, headers={"Location": "https://other.example/steal"}),
        httpx.Response(
            200, json={"access_token": "bad\r\nHeader:x", "token_type": "Bearer", "expires_in": 100}
        ),
        httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Basic", "expires_in": 100}
        ),
        httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 5000}
        ),
        httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Bearer", "expires_in": True}
        ),
        httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 0}
        ),
        httpx.Response(200, content=b"not json PRIVATE BODY"),
        httpx.Response(200, content=b"x" * 70000),
        httpx.Response(200, content=b"[" * 5000 + b"]" * 5000),
    ],
)
@respx.mock
async def test_bad_responses_fail_closed_without_echoing_body(response):
    from coreman.core.auth.token_providers import (
        HTTPTokenProvider,
        TokenProviderError,
        TokenRequest,
    )

    cfg = settings({"company": config()}).business_token_providers["company"]
    respx.post(cfg.token_url).mock(return_value=response)
    with pytest.raises(TokenProviderError) as exc:
        await HTTPTokenProvider(cfg).issue(
            TokenRequest(subject="alice", audience="erp", ttl_seconds=1800)
        )
    assert "PRIVATE BODY" not in str(exc.value) and "secret" not in str(exc.value)
    assert len(respx.calls) == 1


@respx.mock
async def test_custom_field_mapping_and_oauth_basic_encoding():
    from coreman.core.auth.token_providers import HTTPTokenProvider, TokenRequest

    cfg = settings(
        {
            "generic": config(
                subject_field="subject",
                audience_field="resource",
                ttl_field="ttl",
                client_id="a:b",
                client_secret="x y",
            )
        }
    ).business_token_providers["generic"]
    route = respx.post(cfg.token_url).respond(
        200, json={"access_token": "private-token", "token_type": "bearer", "expires_in": 300}
    )
    await HTTPTokenProvider(cfg).issue(
        TokenRequest(subject="bob", audience="inventory", ttl_seconds=600)
    )
    sent = route.calls.last.request
    assert parse_qs(sent.content.decode()) == {
        "subject": ["bob"],
        "resource": ["inventory"],
        "ttl": ["600"],
    }
    assert sent.headers["Authorization"] == "Basic " + base64.b64encode(b"a%3Ab:x+y").decode()


async def test_missing_provider_never_falls_back_to_local_signing():
    from unittest.mock import AsyncMock

    from coreman.core.auth.token_providers import TokenProviderError, issue_system_token
    from coreman.core.db.models import BusinessSystem

    session = AsyncMock()
    with pytest.raises(TokenProviderError, match="provider_not_configured"):
        await issue_system_token(
            session,
            None,
            system=BusinessSystem(key="erp", name="ERP", token_provider="removed"),
            subject="alice",
            name="Alice",
            ttl_seconds=1800,
            issuer="coreman",
            external_key=None,
        )
    assert not session.mock_calls


@respx.mock
async def test_network_errors_are_safe_and_do_not_retry():
    from coreman.core.auth.token_providers import (
        HTTPTokenProvider,
        TokenProviderError,
        TokenRequest,
    )

    cfg = settings({"company": config()}).business_token_providers["company"]
    route = respx.post(cfg.token_url).mock(side_effect=httpx.ReadTimeout("PRIVATE BODY"))
    with pytest.raises(TokenProviderError, match="issuer_unavailable") as exc:
        await HTTPTokenProvider(cfg).issue(TokenRequest("alice", "erp", 1800))
    assert "PRIVATE BODY" not in str(exc.value)
    assert route.call_count == 1


@pytest.mark.parametrize("provider", ["builtin", "company"])
async def test_reserved_platform_audience_cannot_be_issued_to_business_system(provider):
    from unittest.mock import AsyncMock

    from coreman.core.auth.token_providers import TokenProviderError, issue_system_token
    from coreman.core.db.models import BusinessSystem

    session = AsyncMock()
    with pytest.raises(TokenProviderError, match="reserved_audience"):
        await issue_system_token(
            session,
            None,
            system=BusinessSystem(
                key="erp", name="ERP", token_provider=provider, token_audience="coreman"
            ),
            subject="alice",
            name="Alice",
            ttl_seconds=1800,
            issuer="coreman",
            external_key=None,
            providers=settings({"company": config()}).business_token_providers,
        )
    assert not session.mock_calls


@respx.mock
async def test_total_deadline_bounds_a_slow_issuer(monkeypatch):
    import asyncio

    from coreman.core.auth import token_providers

    monkeypatch.setattr(token_providers, "ISSUE_TIMEOUT_SECONDS", 0.01, raising=False)
    cfg = settings({"company": config()}).business_token_providers["company"]

    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(
            200, json={"access_token": "slow-token", "token_type": "Bearer", "expires_in": 100}
        )

    respx.post(cfg.token_url).mock(side_effect=slow)
    with pytest.raises(token_providers.TokenProviderError, match="issuer_unavailable"):
        await token_providers.HTTPTokenProvider(cfg).issue(
            token_providers.TokenRequest("alice", "erp", 1800)
        )


@respx.mock
async def test_provider_never_accepts_a_token_that_bypasses_runtime_redaction():
    from coreman.core.auth.token_providers import (
        HTTPTokenProvider,
        TokenProviderError,
        TokenRequest,
    )
    from coreman.core.chat.redaction import collect_secrets, redact

    cfg = settings({"company": config()}).business_token_providers["company"]
    route = respx.post(cfg.token_url)
    route.respond(200, json={"access_token": "abc123", "token_type": "Bearer", "expires_in": 60})
    with pytest.raises(TokenProviderError, match="invalid_response"):
        await HTTPTokenProvider(cfg).issue(TokenRequest("alice", "erp", 60))
    route.respond(
        200, json={"access_token": "opaque-token", "token_type": "Bearer", "expires_in": 60}
    )
    issued = await HTTPTokenProvider(cfg).issue(TokenRequest("alice", "erp", 60))
    assert "opaque-token" not in redact(
        "Bearer opaque-token", collect_secrets({"BOT_TOKEN_ERP": issued.value})
    )
