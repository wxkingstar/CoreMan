"""Fetch a business system's OpenAPI description.

Only the system's own origin is reachable: the URL must share scheme, host and port with the
system URL, the transport is pinned to that host (no DNS rebinding to metadata addresses), redirects
are not followed and proxy variables in the environment are ignored. The body is capped while it
streams, so an oversized description never sits in memory whole.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from coreman.core.auth.token_providers import IssuedToken
from coreman.core.relay.safe_transport import RegisteredTransport
from coreman.core.systems_catalog.compiler import same_origin
from coreman.core.systems_catalog.loader import MAX_BYTES

TIMEOUT_SECONDS = 15
ACCEPT = "application/json, application/yaml;q=0.9, application/x-yaml;q=0.9, text/yaml;q=0.8"


class FetchError(Exception):
    """A safe error code: never the URL, a response body or a credential."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Fetched:
    not_modified: bool
    body: bytes = b""
    etag: str | None = None


def registered_transport(url: httpx.URL) -> httpx.AsyncBaseTransport:
    port = url.port or (443 if url.scheme == "https" else 80)
    return RegisteredTransport(url.host, port, scheme=url.scheme)


# Tests swap this for a fake business system; production always pins to the registered host.
transport_factory: Callable[[httpx.URL], httpx.AsyncBaseTransport] = registered_transport


def auth_headers(token: IssuedToken) -> dict[str, str]:
    if token.auth_mode == "bearer":
        return {"Authorization": f"Bearer {token.value}"}
    return {"Cookie": f"bot_token={token.value}"}


def client_for(url: httpx.URL, *, timeout: float = TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """A client that can only reach `url`'s origin: no redirects, no environment proxies."""
    if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
        raise FetchError("invalid_url")
    try:
        transport = transport_factory(url)
    except ValueError:
        raise FetchError("address_not_allowed") from None
    return httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False, timeout=timeout
    )


async def fetch_spec(
    spec_url: str, *, base_url: str, token: IssuedToken, etag: str | None
) -> Fetched:
    if not same_origin(spec_url, base_url):
        raise FetchError("cross_origin")
    url = httpx.URL(spec_url)
    if url.fragment:
        raise FetchError("invalid_url")
    headers = {"Accept": ACCEPT, **auth_headers(token)}
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS), client_for(url) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304 and etag:
                    return Fetched(True, etag=etag)
                if 300 <= response.status_code < 400:
                    raise FetchError("redirect_not_followed")
                if response.status_code in (401, 403):
                    raise FetchError("access_denied")
                if response.status_code != 200:
                    raise FetchError(f"http_{response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise FetchError("too_large")
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_BYTES:
                        raise FetchError("too_large")
                new_etag = response.headers.get("etag")
                if new_etag is not None and (len(new_etag) > 512 or not new_etag.isprintable()):
                    new_etag = None
                return Fetched(False, bytes(raw), new_etag)
    except (httpx.HTTPError, TimeoutError):
        raise FetchError("unreachable") from None
