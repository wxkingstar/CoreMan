import asyncio
import socket

import httpx
import pytest

from coreman.core.relay.safe_transport import RegisteredTransport, allowed_address, validate_host


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "x.localhost",
        "metadata.google.internal",
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "::1",
        "0.0.0.0",
        "224.0.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_unsafe_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        validate_host(host)


def test_registered_private_network_is_supported() -> None:
    assert allowed_address("10.0.0.9") and allowed_address("192.168.2.1")


async def test_dns_is_pinned_and_rebinding_or_redirect_cannot_receive_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    responses = ["10.0.0.9", "169.254.169.254"]

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (responses.pop(0), 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    transport = RegisteredTransport("relay.example", 80, transport=httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await client.post(
            "http://relay.example/v1/chat/completions",
            headers={"Authorization": "Bearer synthetic-test"},
            json={},
        )
        assert r.status_code == 302 and len(calls) == 1
        assert calls[0].url.host == "10.0.0.9" and calls[0].headers["host"] == "relay.example"
        with pytest.raises(httpx.ConnectError):
            await client.get("http://relay.example/health")
        with pytest.raises(httpx.ConnectError):
            await client.get("http://other.example/health")
        assert len(calls) == 1


async def test_https_pin_preserves_tls_hostname(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def handle(request):
        assert request.url.host == "10.0.0.9"
        assert request.headers["host"] == "system.example"
        assert request.extensions["sni_hostname"] == "system.example"
        return httpx.Response(200)

    transport = RegisteredTransport(
        "system.example", 443, scheme="https", transport=httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://system.example/")).status_code == 200
        with pytest.raises(httpx.ConnectError):
            await client.get("http://system.example/")
