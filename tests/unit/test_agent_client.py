import uuid

import httpx
import pytest

from coreman.core.db.models import RelayServer
from coreman.core.relay import agent_client


def _relay(**values: object) -> RelayServer:
    return RelayServer(
        runtime_node_id=uuid.uuid4(), model_provider="claude", is_active=True, **values
    )


def _fake_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        agent_client,
        "make_http",
        lambda r: httpx.AsyncClient(base_url="http://runtime", transport=transport),
    )


async def test_agent_requests_reach_node_without_token_and_cannot_override_operation(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        assert "authorization" not in req.headers
        assert b'"type":"status"' in req.content
        return httpx.Response(200, json={"success": True, "count": 0})

    _fake_http(monkeypatch, handler)
    relay = _relay()
    result = await agent_client.call_agent(relay, None, "status", {"type": "pull"})
    assert result["count"] == 0
    relay.is_active = False
    with pytest.raises(agent_client.AgentError):
        await agent_client.call_agent(relay, None, "status")
    for retired in ("mail-probe", "check-active-tasks"):
        with pytest.raises(agent_client.AgentError):
            await agent_client.call_agent(_relay(), None, retired)
    assert len(seen) == 1


async def test_standalone_relay_is_rejected_before_any_request(monkeypatch):
    _fake_http(monkeypatch, lambda req: pytest.fail("must not send"))
    relay = RelayServer(model_provider="claude", is_active=True)
    with pytest.raises(agent_client.AgentError):
        await agent_client.call_agent(relay, None, "ping")
    monkeypatch.undo()
    with pytest.raises(agent_client.AgentError):
        agent_client.make_http(relay)


async def test_agent_error_body_is_never_forwarded(monkeypatch):
    _fake_http(monkeypatch, lambda r: httpx.Response(500, text="synthetic-private-output"))
    with pytest.raises(agent_client.AgentError) as exc:
        await agent_client.call_agent(_relay(), None, "status")
    assert "synthetic-private-output" not in str(exc.value)


async def test_instruction_conflict_code_is_forwarded_without_runtime_text(monkeypatch):
    _fake_http(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "success": False,
                "code": "instructions_conflict",
                "message": "private filesystem text",
            },
        ),
    )
    with pytest.raises(agent_client.AgentError) as caught:
        await agent_client.call_agent(_relay(), None, "workspace-init")
    assert caught.value.code == "instructions_conflict"
    assert "private filesystem" not in str(caught.value)
    assert caught.value.detail is None


async def test_operation_failure_reason_is_forwarded_as_sanitized_detail(monkeypatch):
    message = "Git 来源不在白名单内\x1b[31m\n" + "长" * 300
    _fake_http(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"success": False, "code": "operation_failed", "message": message}
        ),
    )
    with pytest.raises(agent_client.AgentError) as caught:
        await agent_client.call_agent(_relay(), None, "install-skill")
    assert caught.value.code == "operation_failed"
    detail = caught.value.detail
    assert detail is not None and detail.startswith("Git 来源不在白名单内[31m长")
    assert len(detail) == 200 and all(c.isprintable() for c in detail)
    # The platform text stays generic; callers opt in to showing the node's reason.
    assert "白名单" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {"success": False, "message": "private output"},
        {"success": False, "code": "conflict", "message": "private output"},
        {"success": False, "code": "unknown", "message": "private output"},
        {"success": False, "code": "operation_failed", "message": ["not", "text"]},
        {"success": False, "code": "operation_failed", "message": " \n "},
        {"success": False, "code": "operation_failed"},
        ["not", "an", "object"],
    ],
)
async def test_only_operation_failed_messages_become_detail(monkeypatch, body):
    _fake_http(monkeypatch, lambda r: httpx.Response(200, json=body))
    with pytest.raises(agent_client.AgentError) as caught:
        await agent_client.call_agent(_relay(), None, "install-skill")
    assert caught.value.detail is None
    assert caught.value.code in {None, "conflict", "operation_failed"}


async def test_older_node_execution_failure_keeps_its_frame_code(monkeypatch):
    # The reverse transport raises a node's error frame as a ConnectError named after it.
    def failed(request):
        raise httpx.ConnectError("execution_failed", request=request)

    _fake_http(monkeypatch, failed)
    with pytest.raises(agent_client.AgentError) as caught:
        await agent_client.call_agent(_relay(), None, "install-skill")
    assert caught.value.code == "execution_failed" and caught.value.detail is None

    def offline(request):
        raise httpx.ConnectError("运行时离线或未启用", request=request)

    _fake_http(monkeypatch, offline)
    with pytest.raises(agent_client.AgentError) as caught:
        await agent_client.call_agent(_relay(), None, "install-skill")
    assert caught.value.code is None
