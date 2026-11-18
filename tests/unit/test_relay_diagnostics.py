import base64
import errno
import json
import socket
import uuid

import httpx
import pytest

from coreman.core.relay.client import (
    ChatRequest,
    RelayClient,
    RelayConnectionError,
    RelayRequestError,
)
from coreman.core.relay.diagnostics import exception_details
from coreman.core.runtime_nodes.errors import RuntimeRequestRejected
from coreman.core.runtime_nodes.transport import MAX_REQUEST, ReverseTransport
from coreman.runtime.worker.chat.finalize import never_reached_node


async def test_base64_attachment_rejected_before_database_or_runtime(monkeypatch):
    # Production incident: 66,296,816 raw bytes expands past the 64 MiB envelope.
    assert 4 * ((66_296_816 + 2) // 3) > MAX_REQUEST
    monkeypatch.setattr("coreman.core.runtime_nodes.transport.MAX_REQUEST", 1024)
    transport = ReverseTransport(uuid.uuid4(), "codex")
    # No DB factory is available: the rejection must happen before queueing.
    request = ChatRequest(
        "m",
        "s",
        [
            {
                "type": "file_url",
                "file_url": {
                    "url": "data:application/octet-stream;base64,"
                    + base64.b64encode(b"x" * 900).decode(),
                },
            }
        ],
        "/tmp",
        "session",
        "codex",
    )
    client = RelayClient(transport=transport)
    try:
        with pytest.raises(RelayRequestError) as caught:
            [event async for event in client.chat_stream(request, total_timeout=10)]
    finally:
        await client.aclose()
    error = caught.value
    assert error.code == "runtime_request_too_large"
    assert "附件" in str(error) and "连接失败" not in str(error)
    assert never_reached_node(error)
    details = exception_details(error)
    rejection = details["exception_chain"][1]
    assert rejection["type"] == "RuntimeRequestRejected"
    assert rejection["request_bytes"] > rejection["limit_bytes"] == 1024
    assert "data:" not in json.dumps(details)


async def test_connection_failure_keeps_cause_chain_without_credentials():
    def handler(req):
        try:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "private chat Bearer secret")
        except OSError as cause:
            raise httpx.ConnectError(
                "https://user:password@example.test?token=secret", request=req
            ) from cause

    async with httpx.AsyncClient(
        base_url="http://runtime", transport=httpx.MockTransport(handler)
    ) as http:
        client = RelayClient(http=http)
        with pytest.raises(RelayConnectionError) as caught:
            [
                event
                async for event in client.chat_stream(
                    ChatRequest("m", "s", "u", "/tmp", "s", "codex"), total_timeout=5
                )
            ]
    details = exception_details(caught.value)
    assert [x["type"] for x in details["exception_chain"]] == [
        "RelayConnectionError",
        "ConnectError",
        "ConnectionRefusedError",
    ]
    assert details["exception_chain"][-1]["errno_name"] == "ECONNREFUSED"
    assert details["exception_chain"][1]["frames"]
    assert not any(
        word in json.dumps(details) for word in ("password", "secret", "private chat", "token=")
    )


def test_dns_provenance_and_exception_cycles_are_bounded():
    error = socket.gaierror(socket.EAI_NONAME, "a private hostname")
    error.__cause__ = error
    details = exception_details(error)
    assert len(details["exception_chain"]) == 1
    assert details["exception_chain"][0]["reason"] == "dns_resolution_failed"
    assert "private hostname" not in json.dumps(details)


def test_request_validation_is_not_connectivity():
    error = RuntimeRequestRejected(
        "runtime_provider_invalid",
        "运行时 AI 类型无效",
        request=httpx.Request("POST", "http://runtime"),
    )
    assert exception_details(error)["exception_chain"][0]["code"] == "runtime_provider_invalid"
