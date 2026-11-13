"""请求体与调用标记：普通机器人的请求体一字不变；助理的一轮带 sandbox 与 transcript。"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from coreman.core.relay.client import ChatRequest, RelayClient, UnsandboxedPoolRequest
from coreman.core.runtime_nodes.transport import SANDBOX_EXTENSION, TOTAL_TIMEOUT_EXTENSION

BASE = {
    "model": "claude-opus-5-5",
    "system_prompt": "sys",
    "user_content": "hi",
    "working_dir": "/data/demo-bot",
    "session_id": "s1",
    "backend": "claude",
    "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
}


def test_standard_body_is_exactly_what_it_was() -> None:
    assert ChatRequest(**BASE).to_body() == {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "working_dir": "/data/demo-bot",
        "session_id": "s1",
        "max_turns": 200,
        "env_vars": {"COREMAN_BOT_KEY": "demo-bot"},
    }


def test_sandboxed_body_carries_the_handover() -> None:
    handover = {"transfer": "a" * 32, "restore": True}
    body = ChatRequest(**BASE, sandbox=True, transcript=handover).to_body()
    assert body["sandbox"] is True and body["transcript"] == handover
    assert body["transcript"] is not handover


async def test_stream_marks_only_sandboxed_calls() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions))
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = RelayClient("http://relay.test", transport=httpx.MockTransport(handler))
    try:
        for request in (
            ChatRequest(**BASE),
            ChatRequest(**BASE, sandbox=True, transcript={"transfer": "b" * 32, "restore": False}),
        ):
            [event async for event in client.chat_stream(request, total_timeout=30)]
    finally:
        await client.aclose()
    assert TOTAL_TIMEOUT_EXTENSION in seen[0] and SANDBOX_EXTENSION not in seen[0]
    assert seen[1][SANDBOX_EXTENSION] is True


HANDOVER = {"transfer": "c" * 32, "restore": False}


def test_inconsistent_sandbox_and_transcript_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        ChatRequest(**BASE, transcript=HANDOVER)
    with pytest.raises(ValueError):
        ChatRequest(**BASE, sandbox=True)
    ChatRequest(**BASE)
    sandboxed = ChatRequest(**BASE, sandbox=True, transcript=HANDOVER)
    assert dataclasses.replace(sandboxed, user_content="again").sandbox is True
    assert dataclasses.replace(ChatRequest(**BASE), user_content="again").sandbox is False


def _pool_client(seen: list[httpx.Request], *, pool: bool) -> RelayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    return RelayClient(
        "http://relay.test", transport=httpx.MockTransport(handler), assistant_pool=pool
    )


async def test_pool_relay_refuses_an_unsandboxed_request_before_anything_is_sent() -> None:
    seen: list[httpx.Request] = []
    client = _pool_client(seen, pool=True)
    try:
        with pytest.raises(UnsandboxedPoolRequest):
            [e async for e in client.chat_stream(ChatRequest(**BASE), total_timeout=30)]
        assert seen == []
        sandboxed = ChatRequest(**BASE, sandbox=True, transcript=HANDOVER)
        events = [e async for e in client.chat_stream(sandboxed, total_timeout=30)]
        assert events and len(seen) == 1
    finally:
        await client.aclose()


async def test_ordinary_relay_is_unaffected_by_the_tripwire() -> None:
    seen: list[httpx.Request] = []
    client = _pool_client(seen, pool=False)
    try:
        [e async for e in client.chat_stream(ChatRequest(**BASE), total_timeout=30)]
    finally:
        await client.aclose()
    sent = json.loads(seen[0].content)
    assert sent == ChatRequest(**BASE).to_body()
    assert SANDBOX_EXTENSION not in seen[0].extensions
