import asyncio
import json

import pytest

from coreman.core.relay.client import (
    VERBOSITY_OUTPUT_STYLES,
    ChatRequest,
    IncompleteResultError,
    RelayError,
)
from coreman.core.relay.sse import FinishEvent, RelayErrorEvent, TextDelta, ToolUseStart, UsageEvent
from tests.fakes.fake_relay import FakeRelay


def req(**over: object) -> ChatRequest:
    base = dict(
        model="vllm/claude-sonnet-4-6",
        system_prompt="sys",
        user_content="hi",
        working_dir="/d",
        session_id="11111111-1111-4111-8111-111111111111",
        backend="claude",
        effort="high",
        verbosity_level=3,
        env_vars={"A": "1"},
    )
    base.update(over)
    return ChatRequest(**base)  # type: ignore[arg-type]


def test_to_body_claude_and_codex() -> None:
    body = req().to_body()
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_turns"] == 80
    assert json.loads(body["settings"]) == {"outputStyle": VERBOSITY_OUTPUT_STYLES[3]}
    assert body["effort"] == "high" and body["env_vars"] == {"A": "1"}
    assert body["session_id"].startswith("1111")
    codex = req(
        model="codex/gpt-5.5", backend="codex", verbosity_level=1, effort=None, env_vars={}
    ).to_body()
    assert "max_turns" not in codex and "settings" not in codex
    assert "effort" not in codex and "env_vars" not in codex
    assert "settings" not in req(verbosity_level=1).to_body()


async def collect(relay: FakeRelay, **kw: object) -> list[object]:
    client = relay.client()
    try:
        return [e async for e in client.chat_stream(req(), total_timeout=5.0, **kw)]  # type: ignore[arg-type]
    finally:
        await client.aclose()


async def test_normal_scenario_yields_events_and_records_request() -> None:
    relay = FakeRelay("normal")
    events = await collect(relay)
    kinds = [type(e).__name__ for e in events]
    assert kinds.count("ThinkingDelta") == 2 and kinds.count("TextDelta") == 3
    assert ToolUseStart("Bash", "t1") in events
    assert events[-2] == FinishEvent("stop")
    assert events[-1] == UsageEvent(100, 20, 80, 0)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "你好，世界。"
    assert relay.requests[0]["model"] == "vllm/claude-sonnet-4-6"
    assert relay.requests[0]["working_dir"] == "/d"


async def test_relay_error_and_empty() -> None:
    assert [e for e in await collect(FakeRelay("relay_error")) if isinstance(e, RelayErrorEvent)]
    events = await collect(FakeRelay("empty"))
    assert events == [FinishEvent("stop")]


async def test_relay_error_without_finish_is_not_reported_as_incomplete() -> None:
    # 驱动回错后不补 finish chunk：错误事件已经交给调用方，不能再抛「未确认终态」把原话顶掉。
    events = await collect(FakeRelay("relay_error_no_finish"))
    assert [e for e in events if isinstance(e, RelayErrorEvent)]
    with pytest.raises(IncompleteResultError, match="incomplete_result"):
        await collect(FakeRelay("empty_no_finish"))
    with pytest.raises(IncompleteResultError):
        await collect(FakeRelay("no_finish"))


async def test_http_error_connect_error_and_timeouts() -> None:
    with pytest.raises(RelayError, match="HTTP 500"):
        await collect(FakeRelay("http_500"))
    with pytest.raises(RelayError, match="连接失败"):
        await collect(FakeRelay("connect_error"))
    with pytest.raises(RelayError, match="总时长"):
        client = FakeRelay("slow", chunk_delay=0.3).client()
        try:
            [e async for e in client.chat_stream(req(), total_timeout=0.5)]
        finally:
            await client.aclose()


async def test_closing_generator_disconnects() -> None:
    relay = FakeRelay("slow", chunk_delay=0.2)
    client = relay.client()
    gen = client.chat_stream(req(), total_timeout=5.0)
    first = await gen.__anext__()
    assert first is not None
    await gen.aclose()
    await client.aclose()
    await asyncio.sleep(0.3)
    assert relay.aborted == 1
