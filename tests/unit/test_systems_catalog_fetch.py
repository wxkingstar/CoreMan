"""Fetching a description: same origin only, no redirects, no proxies, bounded body."""

from __future__ import annotations

import httpx
import pytest

from coreman.core.auth.token_providers import IssuedToken
from coreman.core.relay.safe_transport import RegisteredTransport
from coreman.core.systems_catalog import fetch
from coreman.core.systems_catalog.loader import MAX_BYTES
from tests.fakes.business_system import BASE_URL, SPEC_URL

BEARER = IssuedToken("bearer-token-value", 300, 2_000_000_000, "bearer")
COOKIE = IssuedToken("cookie-token-value", 300, 2_000_000_000, "cookie")


def serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[no-any-return]

    monkeypatch.setattr(fetch, "transport_factory", lambda url: httpx.MockTransport(record))
    return seen


async def test_cross_origin_description_is_never_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda r: httpx.Response(200, content=b"{}"))
    for url in (
        "https://evil.example/openapi.json",
        "http://stock.example.com/openapi.json",
        "https://stock.example.com:8443/openapi.json",
    ):
        with pytest.raises(fetch.FetchError) as exc:
            await fetch.fetch_spec(url, base_url=BASE_URL, token=BEARER, etag=None)
        assert exc.value.code == "cross_origin"
    assert seen == []


async def test_redirects_are_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"}),
    )
    with pytest.raises(fetch.FetchError) as exc:
        await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag=None)
    assert exc.value.code == "redirect_not_followed"
    assert [str(r.url) for r in seen] == [SPEC_URL]


async def test_body_is_capped_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(MAX_BYTES // 65536 + 2):
            yield b"x" * 65536

    serve(monkeypatch, lambda r: httpx.Response(200, content=chunks()))
    with pytest.raises(fetch.FetchError) as exc:
        await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag=None)
    assert exc.value.code == "too_large"
    serve(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=b"{}", headers={"Content-Length": str(MAX_BYTES + 1)}
        ),
    )
    with pytest.raises(fetch.FetchError) as exc:
        await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag=None)
    assert exc.value.code == "too_large"


async def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://203.0.113.9:3128")
    seen = serve(monkeypatch, lambda r: httpx.Response(200, content=b"{}"))
    result = await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag=None)
    assert result.body == b"{}" and len(seen) == 1


async def test_conditional_request_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda r: httpx.Response(304, headers={"ETag": '"v1"'}))
    result = await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag='"v1"')
    assert result.not_modified and result.etag == '"v1"'
    assert seen[0].headers["If-None-Match"] == '"v1"'
    assert seen[0].headers["Authorization"] == "Bearer bearer-token-value"
    seen = serve(monkeypatch, lambda r: httpx.Response(200, content=b"{}", headers={"ETag": "x"}))
    await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=COOKIE, etag=None)
    assert seen[0].headers["Cookie"] == "bot_token=cookie-token-value"
    assert "If-None-Match" not in seen[0].headers and "Authorization" not in seen[0].headers


@pytest.mark.parametrize(("status", "code"), [(401, "access_denied"), (500, "http_500")])
async def test_error_statuses(monkeypatch: pytest.MonkeyPatch, status: int, code: str) -> None:
    serve(monkeypatch, lambda r: httpx.Response(status, content=b"secret upstream body"))
    with pytest.raises(fetch.FetchError) as exc:
        await fetch.fetch_spec(SPEC_URL, base_url=BASE_URL, token=BEARER, etag=None)
    assert exc.value.code == code and "secret" not in str(exc.value)


def test_default_transport_is_pinned_to_the_registered_host() -> None:
    transport = fetch.registered_transport(httpx.URL(SPEC_URL))
    assert isinstance(transport, RegisteredTransport)
    assert (transport.host, transport.port, transport.scheme) == ("stock.example.com", 443, "https")
    with pytest.raises(fetch.FetchError) as exc:
        fetch.client_for(httpx.URL("http://localhost/openapi.json"))
    assert exc.value.code == "address_not_allowed"
    client = fetch.client_for(httpx.URL(SPEC_URL))
    assert client.follow_redirects is False and client.trust_env is False
