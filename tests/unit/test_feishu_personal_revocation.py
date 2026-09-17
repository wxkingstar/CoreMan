from urllib.parse import parse_qs

import httpx
import pytest

from coreman.core.feishu_personal.revocation import (
    MAX_RESPONSE_BYTES,
    REVOKE_URL,
    revoke_tokens,
)


async def test_revokes_both_tokens_with_official_form_contract():
    forms = []

    def handler(request):
        assert str(request.url) == REVOKE_URL
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        forms.append(parse_qs(request.content.decode()))
        return httpx.Response(204) if len(forms) == 1 else httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await revoke_tokens(
            "app", "secret", {"access_token": "access", "refresh_token": "refresh"}, http=http
        )
        assert not http.is_closed
    assert forms == [
        {
            "client_id": ["app"],
            "client_secret": ["secret"],
            "token": [token],
            "token_type_hint": [hint],
        }
        for hint, token in (("access_token", "access"), ("refresh_token", "refresh"))
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://example.com"}),
        httpx.Response(401, text="synthetic-secret"),
        httpx.Response(200, json={"error": "synthetic-secret"}),
        httpx.Response(200, json={"code": 999, "msg": "synthetic-secret"}),
        httpx.Response(200, text="not json synthetic-secret"),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    ],
)
async def test_failure_still_revokes_second_token_without_logging(response, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return response if len(calls) == 1 else httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        assert not await revoke_tokens(
            "app",
            "synthetic-secret",
            {"access_token": "access", "refresh_token": "refresh"},
            http=http,
        )
    assert len(calls) == 2
    assert "synthetic-secret" not in caplog.text


async def test_transport_failure_does_not_skip_second_token(caplog):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("synthetic-secret")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert not await revoke_tokens(
            "app", "secret", {"access_token": "access", "refresh_token": "refresh"}, http=http
        )
    assert calls == 2
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("tokens", [{}, {"access_token": ""}, {"access_token": 1}])
async def test_no_tokens_cannot_claim_remote_revocation(tokens):
    assert not await revoke_tokens("app", "secret", tokens)
