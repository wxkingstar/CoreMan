"""脚本化的 运行时 假服务（httpx.MockTransport），供 worker/relay 客户端测试回放 SSE。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from coreman.core.relay.client import RelayClient


def _chunk(
    delta: dict[str, Any] | None,
    *,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    error: bool = False,
) -> str:
    body: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
    }
    body["choices"] = (
        []
        if delta is None and usage is not None
        else [{"index": 0, "delta": delta or {"role": "", "content": ""}, "finish_reason": finish}]
    )
    if usage is not None:
        body["usage"] = usage
    if error:
        body["x_relay_error"] = True
    return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"


USAGE = {
    "prompt_tokens": 180,
    "completion_tokens": 20,
    "total_tokens": 200,
    "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 0},
}
FINISH = _chunk({"role": "", "content": ""}, finish="stop")
DONE = "data: [DONE]\n\n"


def _text(t: str) -> str:
    return _chunk({"role": "assistant", "content": t})


def _think(t: str) -> str:
    return _chunk({"role": "assistant", "content": None, "thinking": t})


def _tool(name: str, tid: str, args: str = "") -> str:
    return _chunk(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": tid, "type": "function", "function": {"name": name, "arguments": args}}
            ],
        }
    )


SCENARIOS: dict[str, Callable[[], list[str]]] = {
    "normal": lambda: [
        ": ping\n\n",
        _think("先想"),
        _think("再想"),
        _text("你好"),
        _text("，世界"),
        _tool("Bash", "t1"),
        _text("。"),
        FINISH,
        _chunk(None, usage=USAGE),
        DONE,
    ],
    "tools_only": lambda: [
        ": ping\n\n",
        _tool("Bash", "t1"),
        _tool("Read", "t2"),
        FINISH,
        _chunk(None, usage=USAGE),
        DONE,
    ],
    "empty": lambda: [": ping\n\n", FINISH, DONE],
    "no_finish": lambda: [_text("半截"), DONE],
    "no_usage": lambda: [_text("hi"), FINISH, DONE],
    "relay_error": lambda: [
        ": ping\n\n",
        _chunk(
            {"role": "assistant", "content": "⚠️ claude exited without producing any output"},
            error=True,
        ),
        _chunk({"role": "", "content": ""}, finish="stop", error=True),
        DONE,
    ],
    # 驱动在 SSE 已开之后失败，只回错误块、不带 finish chunk（旧版 codex 首行零输出的形状）。
    "relay_error_no_finish": lambda: [
        ": ping\n\n",
        _chunk(
            {
                "role": "assistant",
                "content": "\n\n[codex error] codex produced no output: exit status 1",
            },
            error=True,
        ),
        DONE,
    ],
    # 零事件流：只有心跳与 [DONE]，既无正文也无 finish chunk。
    "empty_no_finish": lambda: [": ping\n\n", DONE],
    "codex_error": lambda: [_text("部分输出"), _text("\n\n[codex error] boom"), FINISH, DONE],
    "codex_normal": lambda: [
        ": ping\n\n",
        _think("整段推理"),
        _tool("shell", "item1", '{"command":"ls"}'),
        _text("整条回复"),
        FINISH,
        _chunk(None, usage={"prompt_tokens": 50, "completion_tokens": 5}),
        DONE,
    ],
    "ask_user": lambda: [
        _text("先说一句"),
        _tool(
            "AskUserQuestion",
            "t9",
            json.dumps(
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
            ),
        ),
        FINISH,
        DONE,
    ],
    # 提交轮（choice_submit）：答案回给模型后又查了一轮才给结论，正好有一个工具边界可分段。
    "submit_round": lambda: [
        _text("先查数据"),
        _tool("Bash", "t1"),
        _text("结论：选 B"),
        FINISH,
        _chunk(None, usage=USAGE),
        DONE,
    ],
    # 提交轮里模型又提了一个问题：待答状态要在同一个作用域上重建。
    "nested_ask_user": lambda: [
        _text("再确认一下"),
        _tool(
            "AskUserQuestion",
            "t10",
            json.dumps(
                {
                    "questions": [
                        {
                            "question": "确定吗？",
                            "header": "",
                            "options": [{"label": "是", "description": ""}],
                            "multiSelect": False,
                        }
                    ]
                }
            ),
        ),
        FINISH,
        DONE,
    ],
    # relay 撞上额度限制时把 Anthropic 的英文告示原样当正文回来（spec §8.8）。
    "rate_limit": lambda: [_text("You've hit your limit · resets 3pm"), FINISH, DONE],
    "slow": lambda: SCENARIOS["normal"](),
}


class _Stream(httpx.AsyncByteStream):
    """按脚本吐帧的响应体，顺便记账「调用方没把帧收完就走了」。"""

    def __init__(self, relay: FakeRelay, frames: list[str]) -> None:
        self._relay, self._frames = relay, frames
        self._sent = 0
        self._counted = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            if self._relay.first_byte_delay:
                await asyncio.sleep(self._relay.first_byte_delay)
            for frame in self._frames:
                self._sent += 1
                yield frame.encode()
                if self._relay.chunk_delay:
                    await asyncio.sleep(self._relay.chunk_delay)
        except (asyncio.CancelledError, GeneratorExit):
            self._abort()
            raise

    async def aclose(self) -> None:
        # httpx 关响应时必调；帧没发完就是调用方提前断开（GeneratorExit 那条路可能不触发）。
        self._abort()

    def _abort(self) -> None:
        if self._counted or self._sent >= len(self._frames):
            return
        self._counted = True
        self._relay.aborted += 1


class FakeRelay:
    """一个假的 relay 实例：/health、/v1/models 与脚本化的流式对话。

    Attributes:
        requests: 收到的 /v1/chat/completions 请求体
        aborted: 调用方在脚本放完前断开的次数（每条流最多记一次）
    """

    def __init__(
        self,
        scenario: str = "normal",
        *,
        first_byte_delay: float = 0.0,
        chunk_delay: float = 0.0,
        base_url: str = "http://relay.test",
    ) -> None:
        self.scenario = scenario
        self.first_byte_delay = first_byte_delay
        self.chunk_delay = chunk_delay
        self.base_url = base_url
        self.requests: list[dict[str, Any]] = []
        self.aborted = 0
        self.transport = httpx.MockTransport(self._handle)

    def client(self) -> RelayClient:
        """建一个接到本假服务上的 RelayClient。"""
        return RelayClient(
            self.base_url, http=httpx.AsyncClient(base_url=self.base_url, transport=self.transport)
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "healthy", "backend": "claude", "version": "fake", "mode": "v1"},
            )
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "vllm/claude-sonnet-4-6"},
                        {"id": "vllm/claude-opus-4-6"},
                        {"id": "codex/gpt-5.5"},
                    ]
                },
            )
        if self.scenario == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if self.scenario == "http_500":
            return httpx.Response(500, text="boom")
        self.requests.append(json.loads(request.content or b"{}"))
        frames = SCENARIOS[self.scenario]()
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_Stream(self, frames)
        )
