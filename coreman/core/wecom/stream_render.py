"""task_streams → 企微 stream.content（spec §6.3、§7.2；限制来自 平台协议 §3.5、§5.7）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from coreman.core.db.models import TaskStream
from coreman.core.i18n.messages import msg

MAX_CONTENT_BYTES = 20480
LONG_RUN_SECONDS = 60

_OPEN_TAG = "<think>\n"
_CLOSE_TAG = "\n</think>\n\n"
# 思考区被压到比这还小就不值得保留了，整块丢弃改为截正文。
_MIN_THINKING_BYTES = 16


@dataclass
class StreamView:
    """渲染只依赖这几个字段；worker 与网关都从 task_streams 行投影出来。"""

    thinking_md: str
    pending_text: str
    final_text: str | None
    is_complete: bool
    running_since: datetime
    session_url: str | None

    @classmethod
    def from_row(cls, row: TaskStream) -> StreamView:
        return cls(
            row.thinking_md,
            row.pending_text,
            row.final_text,
            row.is_complete,
            row.running_since,
            row.session_url,
        )


def truncate_utf8(text: str, max_bytes: int, *, keep: Literal["head", "tail"] = "head") -> str:
    """按 UTF-8 字节数截断，落在字符边界上；`keep` 决定保留开头还是结尾。"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    chunk = data[:max_bytes] if keep == "head" else data[-max_bytes:]
    return chunk.decode("utf-8", errors="ignore")


def render_wecom_stream(
    view: StreamView, now: datetime, locale: str = "zh", *, suffix: str = ""
) -> str:
    """把一行流状态渲染成企微 stream.content；结果保证 ≤ MAX_CONTENT_BYTES 字节。

    `suffix`（排空提示、切后台的 `finish_suffix`）的字节先从预算里扣掉，正文按剩下的额度截断，
    尾缀最后拼上——调用方**不要**在外面拼，否则拼完就超了 20480 字节的硬限制。
    """
    limit = max(MAX_CONTENT_BYTES - _nbytes(suffix), 0)
    out = _render(view, now, locale, limit) + suffix
    # 只有尾缀本身就撑爆预算才会走到这里；宁可截尾缀也不能超限。
    return out if _nbytes(out) <= MAX_CONTENT_BYTES else truncate_utf8(out, MAX_CONTENT_BYTES)


def _render(view: StreamView, now: datetime, locale: str, limit: int) -> str:
    thinking = view.thinking_md
    if view.is_complete:
        text = view.final_text or ""
        if not thinking and not text:
            return truncate_utf8(msg("processing_done", locale), limit)
        indicator = ""
    elif view.pending_text:
        # 有正文了才闭合思考块，并挂运行指示器。
        text = view.pending_text
        elapsed = (now - view.running_since).total_seconds()
        key = "running_indicator_long" if elapsed >= LONG_RUN_SECONDS else "running_indicator"
        indicator = msg(key, locale)
    else:
        # 还没有正文：`<think>` 不闭合，企微据此显示「正在思考」，且不加指示器。
        thinking = thinking or msg("thinking_start", locale)
        return _fit(thinking, "", "", view.session_url, locale, limit, open_think=True)
    return _fit(thinking, text, indicator, view.session_url, locale, limit, open_think=False)


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _compose(thinking: str, text: str, indicator: str, *, open_think: bool) -> str:
    if open_think:
        return _OPEN_TAG + thinking
    if thinking and text:
        return _OPEN_TAG + thinking + _CLOSE_TAG + text + indicator
    if thinking:
        return f"{_OPEN_TAG}{thinking}\n</think>{indicator}"
    return f"{text}{indicator}"


def _fit(
    thinking: str,
    text: str,
    indicator: str,
    session_url: str | None,
    locale: str,
    limit: int,
    *,
    open_think: bool,
) -> str:
    """超限时：优先保正文完整，思考区只留尾部并在头部插入截断标记。"""
    full = _compose(thinking, text, indicator, open_think=open_think)
    if _nbytes(full) <= limit:
        return full

    suffix = msg("truncated_suffix", locale, url=session_url) if session_url else ""
    marker = msg("thinking_truncated", locale)
    fixed = _nbytes(_OPEN_TAG)
    if not open_think:
        fixed += _nbytes(_CLOSE_TAG) + _nbytes(text) + _nbytes(indicator)

    def build(budget: int) -> str:
        room = budget - fixed - _nbytes(marker) - 1
        if room >= _MIN_THINKING_BYTES:
            kept = marker + "\n" + truncate_utf8(thinking, room, keep="tail")
            return _compose(kept, text, indicator, open_think=open_think)
        if open_think:
            # 连标记都放不下（只可能是尾缀异常长），退化为纯尾部保留。
            kept = truncate_utf8(thinking, max(budget - _nbytes(_OPEN_TAG), 0), keep="tail")
            return _compose(kept, "", "", open_think=True)
        # 正文自身就超预算：丢掉思考区，正文保留开头。
        body = truncate_utf8(text, max(budget - _nbytes(indicator), 0))
        return _compose("", body, indicator, open_think=False)

    budget = limit - _nbytes(suffix)
    out = build(budget) + suffix
    # 兜底：标记/尾缀的字节账有偏差时继续收紧，保证硬上限。
    while _nbytes(out) > limit and budget > 0:
        budget = max(budget - max(_nbytes(out) - limit, 1), 0)
        out = build(budget) + suffix
    if _nbytes(out) > limit:
        # 只有尾缀本身就撑爆预算（session_url 异常长）才会走到这里；宁可丢尾缀也不能超限。
        out = truncate_utf8(out, limit)
    return out
