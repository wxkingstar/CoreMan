import httpx
import pytest

from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer
from coreman.core.relay import agent_client


async def test_agent_requests_use_instance_token_and_cannot_override_operation(monkeypatch):
    cipher = Cipher(b"\x05" * 32)
    relay = RelayServer(
        host="10.0.0.9",
        agent_port=52123,
        is_active=True,
        agent_token_enc=cipher.encrypt("synthetic-token", "relay_servers.agent_token_enc"),
    )

    def handler(req):
        assert req.headers["Authorization"] == "Bearer synthetic-token"
        assert b'"type":"status"' in req.content
        return httpx.Response(200, json={"success": True, "count": 0})

    monkeypatch.setattr(
        agent_client,
        "make_http",
        lambda r: httpx.AsyncClient(
            base_url="http://agent.test", transport=httpx.MockTransport(handler)
        ),
    )
    result = await agent_client.call_agent(relay, cipher, "status", {"type": "pull"})
    assert result["count"] == 0
    relay.is_active = False
    with pytest.raises(agent_client.AgentError):
        await agent_client.call_agent(relay, cipher, "status")


async def test_agent_error_body_is_never_forwarded(monkeypatch):
    cipher = Cipher(b"\x05" * 32)
    relay = RelayServer(
        is_active=True,
        agent_token_enc=cipher.encrypt("synthetic-token", "relay_servers.agent_token_enc"),
    )
    monkeypatch.setattr(
        agent_client,
        "make_http",
        lambda r: httpx.AsyncClient(
            base_url="http://agent.test",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(500, text="synthetic-private-output")
            ),
        ),
    )
    with pytest.raises(agent_client.AgentError) as exc:
        await agent_client.call_agent(relay, cipher, "status")
    assert "synthetic-private-output" not in str(exc.value)
