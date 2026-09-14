import json

from coreman.core.relay.sse import (
    AskUserQuestionEvent,
    FinishEvent,
    RelayErrorEvent,
    SseParser,
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
    UsageEvent,
)


def chunk(
    delta: dict | None = None,
    *,
    finish: str | None = None,
    usage: dict | None = None,
    extra: dict | None = None,
) -> str:
    body: dict = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    body["choices"] = (
        []
        if usage is not None and delta is None
        else [{"index": 0, "delta": delta or {"role": "", "content": ""}, "finish_reason": finish}]
    )
    if usage is not None:
        body["usage"] = usage
    body.update(extra or {})
    return "data: " + json.dumps(body, ensure_ascii=False)


def test_text_thinking_tool_usage_finish() -> None:
    p = SseParser("claude")
    assert p.feed_line(": ping") == [] and p.feed_line("") == [] and p.feed_line("garbage") == []
    assert p.feed_line("data: {not json") == []
    assert p.feed_line(chunk({"role": "assistant", "content": "你好"})) == [TextDelta("你好")]
    assert p.feed_line(chunk({"role": "assistant", "content": None, "thinking": "想"})) == [
        ThinkingDelta("想")
    ]
    assert p.feed_line(
        chunk(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "Bash", "arguments": ""}}
                ],
            }
        )
    ) == [ToolUseStart("Bash", "t1")]
    assert p.feed_line(chunk({"role": "", "content": ""}, finish="stop")) == [FinishEvent("stop")]
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "total_tokens": 1020,
        "prompt_tokens_details": {"cached_tokens": 800, "cache_creation_tokens": 100},
    }
    assert p.feed_line(chunk(None, usage=usage)) == [UsageEvent(100, 20, 800, 100)]
    assert p.feed_line("data: [DONE]") == [] and p.done and p.saw_finish
    assert p.counts == {"text": 1, "thinking": 1, "tool": 1, "usage": 1}
    assert p.flush() == []


def test_usage_without_details_and_negative_clamp() -> None:
    p = SseParser("claude")
    assert p.feed_line(chunk(None, usage={"prompt_tokens": 5, "completion_tokens": 1})) == [
        UsageEvent(5, 1, 0, 0)
    ]
    assert p.feed_line(
        chunk(
            None,
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 9},
            },
        )
    ) == [UsageEvent(0, 1, 9, 0)]


def test_relay_error_and_codex_markers() -> None:
    p = SseParser("claude")
    ev = p.feed_line(
        chunk(
            {"role": "assistant", "content": "⚠️ crashed", "thinking": "x"},
            extra={"x_relay_error": True},
        )
    )
    assert ev == [RelayErrorEvent("⚠️ crashed")]  # 带标记的 chunk 只产出错误事件
    c = SseParser("codex")
    assert c.feed_line(chunk({"role": "assistant", "content": "\n\n[codex error] boom"})) == [
        RelayErrorEvent("\n\n[codex error] boom")
    ]
    assert c.feed_line(chunk({"role": "assistant", "content": "⚠️ [codex] 进程异常退出: x"})) == [
        RelayErrorEvent("⚠️ [codex] 进程异常退出: x")
    ]
    assert c.feed_line(chunk({"role": "assistant", "content": "正常"})) == [TextDelta("正常")]
    assert SseParser("claude").feed_line(
        chunk({"role": "assistant", "content": "\n\n[codex error] x"})
    ) == [TextDelta("\n\n[codex error] x")]


def test_ask_user_question_accumulates_and_flushes() -> None:
    p = SseParser("claude")
    args = json.dumps(
        {
            "questions": [
                {
                    "question": "用哪个？",
                    "header": "h",
                    "options": [{"label": "A", "description": ""}],
                    "multiSelect": False,
                }
            ]
        }
    )
    first = chunk(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "t9",
                    "type": "function",
                    "function": {"name": "AskUserQuestion", "arguments": args[:10]},
                }
            ],
        }
    )
    rest = chunk(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t9", "type": "function", "function": {"arguments": args[10:]}}],
        }
    )
    assert p.feed_line(first) == [] and p.feed_line(rest) == []
    assert p.feed_line(chunk(None, finish="stop")) == [FinishEvent("stop")]
    assert p.feed_line("data: [DONE]") == []
    (ev,) = p.flush()
    assert isinstance(ev, AskUserQuestionEvent) and ev.tool_call_id == "t9"
    assert ev.questions[0]["question"] == "用哪个？"
    assert p.flush() == []


def test_ask_user_bad_json_flushes_nothing() -> None:
    p = SseParser("claude")
    p.feed_line(
        chunk(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t",
                        "type": "function",
                        "function": {"name": "AskUserQuestion", "arguments": "{bad"},
                    }
                ],
            }
        )
    )
    assert p.flush() == []
