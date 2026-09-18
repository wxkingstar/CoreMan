import json

import httpx
import pytest

from coreman.core.wecom_bots import scan, verify
from tests.fakes.fake_wecom_ws import FakeWeComWs

SCODE = "Ab12Cd34Ef56Gh78"
AUTH_URL = f"https://work.weixin.qq.com/ai/qc/c?s={SCODE}&hide_more_btn=true&for_native=true"


def _client(handler):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(base_url=scan.BASE_URL, transport=httpx.MockTransport(handler))


def _json(body: object, status: int = 200):  # type: ignore[no-untyped-def]
    return lambda request: httpx.Response(status, json=body)


async def test_generate_calls_cli_source_as_linux_and_returns_qr_content():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"data": {"scode": SCODE, "auth_url": AUTH_URL}})

    async with _client(handler) as http:
        generated = await scan.generate(http=http)
    assert generated == scan.Generated(SCODE, AUTH_URL)
    assert seen[0].path == "/ai/qc/generate"
    assert dict(seen[0].params) == {"source": "wecom_cli_external", "plat": "3"}


@pytest.mark.parametrize(
    "data",
    [
        {"scode": SCODE},
        {"scode": "short", "auth_url": AUTH_URL.replace(SCODE, "short")},
        {"scode": SCODE, "auth_url": "http://work.weixin.qq.com/ai/qc/c?s=" + SCODE},
        {"scode": SCODE, "auth_url": "https://evil.example.com/ai/qc/c?s=" + SCODE},
        {"scode": SCODE, "auth_url": "https://work.weixin.qq.com/ai/qc/c?s=other1234"},
    ],
)
async def test_generate_rejects_unexpected_shapes(data):
    async with _client(_json({"data": data})) as http:
        with pytest.raises(scan.ScanError) as caught:
            await scan.generate(http=http)
    assert caught.value.code == "unexpected_response"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(502, text="bad gateway"), "upstream_unavailable"),
        (httpx.Response(200, json={"errcode": 45009, "errmsg": "freq"}), "upstream_unavailable"),
        (httpx.Response(200, text="<html>"), "unexpected_response"),
        (httpx.Response(200, json=["data"]), "unexpected_response"),
        (httpx.Response(200, json={"status": "init"}), "unexpected_response"),
        (httpx.Response(200, content=b"{" + b" " * (70 * 1024) + b"}"), "unexpected_response"),
    ],
)
async def test_transport_failures_are_classified(response, code):
    async with _client(lambda request: response) as http:
        with pytest.raises(scan.ScanError) as caught:
            await scan.query(SCODE, http=http)
    assert caught.value.code == code


async def test_network_error_is_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom")

    async with _client(handler) as http:
        with pytest.raises(scan.ScanError) as caught:
            await scan.generate(http=http)
    assert caught.value.code == "upstream_unavailable"


@pytest.mark.parametrize("status", ["init", "pending"])
async def test_query_waiting_states(status):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"data": {"status": status}})

    async with _client(handler) as http:
        result = await scan.query(SCODE, http=http)
    assert result == scan.Query("waiting", status)
    assert seen[0].path == "/ai/qc/query_result" and dict(seen[0].params) == {"scode": SCODE}


@pytest.mark.parametrize("key", ["botid", "bot_id"])
async def test_query_returns_credentials_once_bot_is_bound(key):
    body = {"data": {"status": "success", "bot_info": {key: "aib-1", "secret": "s3cret"}}}
    async with _client(_json(body)) as http:
        result = await scan.query(SCODE, http=http)
    assert result == scan.Query("succeeded", "success", "aib-1", "s3cret")


@pytest.mark.parametrize(
    "data",
    [
        {"status": "success"},
        {"status": "success", "bot_info": {"botid": "aib-1"}},
        {"status": "expired"},
        {"status": "Weird Status"},
        {"state": "init"},
    ],
)
async def test_query_flags_unknown_results_as_possible_api_change(data):
    async with _client(_json({"data": data})) as http:
        with pytest.raises(scan.ScanError) as caught:
            await scan.query(SCODE, http=http)
    assert caught.value.code == "unexpected_response"


async def test_verify_subscribes_once_and_disconnects():
    fake = FakeWeComWs(accepted={"aib-1": "good"})
    url = await fake.start()
    try:
        await verify.verify_credentials("aib-1", "good", url=url, timeout=5)
        with pytest.raises(verify.VerifyError) as caught:
            await verify.verify_credentials("aib-1", "wrong", url=url, timeout=5)
    finally:
        await fake.stop()
    assert caught.value.code == "rejected" and caught.value.errcode == 853000
    subscribes = [f for f in fake.frames if f.get("cmd") == "aibot_subscribe"]
    assert [f["body"] for f in subscribes] == [
        {"bot_id": "aib-1", "secret": "good"},
        {"bot_id": "aib-1", "secret": "wrong"},
    ]
    # 校验连接用完即断，不占着机器人唯一的长连接。
    assert "aib-1" not in fake.connections or fake.connections["aib-1"].state.name == "CLOSED"
    assert "wrong" not in str(caught.value)


async def test_verify_unreachable_or_silent_server_is_unavailable(unused_tcp_port: int):
    with pytest.raises(verify.VerifyError) as caught:
        await verify.verify_credentials("b", "s", url=f"ws://127.0.0.1:{unused_tcp_port}")
    assert caught.value.code == "unavailable"

    from websockets.asyncio.server import serve

    async def silent(ws):  # type: ignore[no-untyped-def]
        await ws.recv()
        await ws.wait_closed()

    async with serve(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(verify.VerifyError) as caught:
            await verify.verify_credentials("b", "s", url=f"ws://127.0.0.1:{port}", timeout=0.3)
    assert caught.value.code == "unavailable"


def test_errors_never_carry_secrets():
    assert json.dumps(str(verify.VerifyError("rejected", 853000))) == '"rejected:853000"'
