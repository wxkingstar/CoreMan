"""Bounded progress rendering for the chat stream."""

from __future__ import annotations

import time
from collections.abc import Callable

GENERATING_TAIL_CHARS = 200


class ThinkingCollector:
    """Collect rendered progress lines and a bounded tail of generated text."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.started_at = clock()
        self._lines: list[str] = []
        self._tail: str | None = None

    def _flush(self) -> None:
        if self._tail is not None:
            self._lines.append("💭 " + self._tail)
            self._tail = None

    def add_start(self, content: str) -> None:
        self._flush()
        self._lines.append("🤔 " + content)

    def add_tool_call(self, name: str) -> None:
        self._flush()
        self._lines.append(f"🔧 **{name}**")

    def add_generating(self, text: str) -> None:
        self._tail = ((self._tail or "") + text)[-GENERATING_TAIL_CHARS:]

    def add_end(self, content: str, now: float | None = None) -> None:
        self._flush()
        duration = (self.clock() if now is None else now) - self.started_at
        self._lines.append(f"✨ {content}（总耗时{duration:.0f}s）")

    @property
    def step_count(self) -> int:
        return len(self._lines) + int(self._tail is not None)

    def to_markdown(self) -> str:
        lines = self._lines if self._tail is None else [*self._lines, "💭 " + self._tail]
        return "\n".join(lines)
