"""运行时 SSE 事件解析（report-clawrelay §2、§8.1、§11）。

relay 只发 `: 注释` 与 `data: <json>` 两种帧，以 `data: [DONE]` 收尾；每个 chunk 都带
`finish_reason`（结束前恒为 null），`delta.content` 可能是 `null`/`""`，usage chunk 的
`choices` 是空数组。解析器对任何不认识的行保持沉默（跳过），只把认得的语义吐成事件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# codex 后端没有 x_relay_error，只能靠正文里的这两个标记判定异常（report-clawrelay §11）。
CODEX_ERROR_MARKERS = ("\n\n[codex error] ", "⚠️ [codex] 进程异常退出")


@dataclass(frozen=True)
class TextDelta:
    """一段正文增量。"""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """一段思考过程增量。"""

    text: str


@dataclass(frozen=True)
class ToolUseStart:
    """一次工具调用开始。"""

    name: str
    tool_id: str


@dataclass(frozen=True)
class AskUserQuestionEvent:
    """AskUserQuestion 工具的完整问卷（arguments 拼完后才产出）。"""

    tool_call_id: str
    questions: list[dict[str, Any]]


@dataclass(frozen=True)
class UsageEvent:
    """一次用量统计；input_tokens 已扣除缓存部分。"""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True)
class RelayErrorEvent:
    """relay 以正文形式回传的错误（x_relay_error 或 codex 标记）。"""

    text: str


@dataclass(frozen=True)
class FinishEvent:
    """finish_reason 非空的收尾 chunk。"""

    reason: str


SseEvent = (
    TextDelta
    | ThinkingDelta
    | ToolUseStart
    | AskUserQuestionEvent
    | UsageEvent
    | RelayErrorEvent
    | FinishEvent
)


@dataclass
class SseParser:
    """按行喂入的 SSE 解析器；一个流一个实例。

    Attributes:
        backend: claude / codex，决定是否检查 codex 错误标记
        done: 是否见过 `data: [DONE]`
        saw_finish: 是否见过非空 finish_reason
        saw_error: 是否产出过 RelayErrorEvent（relay 以正文回传的错误）
        counts: text/thinking/tool/usage 事件计数（用于收尾判空）
    """

    backend: str
    done: bool = False
    saw_finish: bool = False
    saw_error: bool = False
    counts: dict[str, int] = field(
        default_factory=lambda: {"text": 0, "thinking": 0, "tool": 0, "usage": 0}
    )
    _ask_id: str | None = None
    _ask_args: str = ""

    def feed_line(self, line: str) -> list[SseEvent]:
        """喂一行，返回这一行产生的事件（顺序：usage、正文/错误、思考、工具、finish）。"""
        line = line.rstrip("\r\n")
        if not line or line.startswith(":") or not line.startswith("data: "):
            return []
        data = line[6:].strip()
        if data == "[DONE]":
            self.done = True
            return []
        try:
            chunk: Any = json.loads(data)
        except ValueError:
            return []
        if not isinstance(chunk, dict):
            return []
        out: list[SseEvent] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            out.append(self._usage(usage))
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return out
        choice = choices[0]
        delta = _obj(choice.get("delta"))
        content = delta.get("content")
        if isinstance(content, str) and content:
            if chunk.get("x_relay_error") or (
                self.backend == "codex" and any(m in content for m in CODEX_ERROR_MARKERS)
            ):
                self.saw_error = True
                out.append(RelayErrorEvent(content))
                return out
            self.counts["text"] += 1
            out.append(TextDelta(content))
        thinking = delta.get("thinking")
        if isinstance(thinking, str) and thinking:
            self.counts["thinking"] += 1
            out.append(ThinkingDelta(thinking))
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = _obj(call.get("function"))
            name, args = fn.get("name"), fn.get("arguments")
            if name == "AskUserQuestion":
                self._ask_id, self._ask_args = str(call.get("id") or ""), str(args or "")
            elif name:
                self.counts["tool"] += 1
                out.append(ToolUseStart(str(name), str(call.get("id") or name)))
            elif self._ask_id is not None and isinstance(args, str):
                self._ask_args += args
        reason = choice.get("finish_reason")
        if reason:
            self.saw_finish = True
            out.append(FinishEvent(str(reason)))
        return out

    def flush(self) -> list[SseEvent]:
        """流结束时把累积的 AskUserQuestion 吐出来；arguments 不成 JSON 则丢弃。"""
        if self._ask_id is None:
            return []
        ask_id, raw = self._ask_id, self._ask_args
        self._ask_id, self._ask_args = None, ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        questions = parsed.get("questions") if isinstance(parsed, dict) else None
        if not isinstance(questions, list):
            return []
        return [AskUserQuestionEvent(ask_id, [q for q in questions if isinstance(q, dict)])]

    def _usage(self, usage: dict[str, Any]) -> UsageEvent:
        self.counts["usage"] += 1
        details = _obj(usage.get("prompt_tokens_details"))
        cached = int(details.get("cached_tokens") or 0)
        creation = int(details.get("cache_creation_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        return UsageEvent(
            max(prompt - cached - creation, 0),
            int(usage.get("completion_tokens") or 0),
            cached,
            creation,
        )


def _obj(value: Any) -> dict[str, Any]:
    """不是 JSON 对象就当空对象，省得每处都写 isinstance。"""
    return value if isinstance(value, dict) else {}
