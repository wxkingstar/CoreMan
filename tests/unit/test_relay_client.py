import httpx

from coreman.core.relay.client import ChatRequest, RelayClient, RelayError, relay_base_url


def _client(handler):  # type: ignore[no-untyped-def]
    return RelayClient(
        "http://relay.test:50009",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay.test:50009"
        ),
    )


async def test_health_healthy_and_models() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "healthy", "backend": "claude", "version": "2.2.0", "mode": "v1"},
            )
        assert req.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "vllm/claude-sonnet-4-6"}, {"id": "codex/gpt-5.5"}],
            },
        )

    c = _client(handler)
    h = await c.health()
    assert (h.status, h.backend, h.version, h.mode) == ("healthy", "claude", "2.2.0", "v1")
    assert h.latency_ms >= 0
    assert await c.models() == ["vllm/claude-sonnet-4-6", "codex/gpt-5.5"]


async def test_health_down_timeout_auth_fail() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Claude CLI not available: boom")

    h = await _client(down).health()
    assert h.status == "down" and h.detail is not None and "503" in h.detail and "boom" in h.detail

    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    assert (await _client(timeout).health()).status == "timeout"

    def auth(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "detail": "not logged in, run /login"})

    assert (await _client(auth).health()).status == "auth_fail"

    def html(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    assert (await _client(html).health()).status == "down"


async def test_models_error_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x")

    try:
        await _client(handler).models()
        raise AssertionError("应抛 RelayError")
    except RelayError:
        pass


def test_relay_base_url() -> None:
    assert relay_base_url("10.0.0.5", 50009) == "http://10.0.0.5:50009"


def test_to_body_accepts_content_parts() -> None:
    parts = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    req = ChatRequest(
        model="vllm/claude-sonnet-4-6",
        system_prompt="s",
        user_content=parts,
        working_dir="/d",
        session_id="x",
        backend="claude",
    )
    assert req.to_body()["messages"][1] == {"role": "user", "content": parts}
