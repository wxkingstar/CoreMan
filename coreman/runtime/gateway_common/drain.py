"""网关整实例排空的节奏：分批错开、批内有界并发，总时长落在停机宽限内。

逐个 bot、间隔 3 秒串行排空时，一台实例在 120 秒宽限里只排得完约 40 个 bot，超出的被
SIGKILL 硬接管（流不补 finish、答案经出站箱整段重发）。这里保留「批与批间隔 3 秒」，
避免把一整片重连同时砸到新实例；每批几个按「剩余 bot ÷ 剩余时间还能排几批」动态算，
单批不超过 `DRAIN_MAX_BATCH`（约等于进程连接池上限，排空的每一步都要短暂借连接）。
bot 少的时候每批 1 个，与原来的串行节奏一致。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence

from coreman.core.logging import get_logger

log = get_logger(__name__)

DRAIN_GAP_SECONDS = 3.0
DRAIN_MAX_BATCH = 10


def batch_size(remaining: int, time_left: float, *, gap: float, max_batch: int) -> int:
    """还剩 `remaining` 个 bot、`time_left` 秒：每 `gap` 秒一批，这一批要排几个才排得完。"""
    if remaining <= 0:
        return 0
    rounds = max(1, int(time_left // gap)) if gap > 0 else 1
    return max(1, min(max_batch, -(-remaining // rounds)))


def shutdown_budget(stop_grace: float, *, reserve: float = 10.0) -> float:
    """停机宽限里留给排空的秒数：扣掉收尾（标记实例停止、关监听、释放连接池）的余量。"""
    return max(0.0, stop_grace - min(reserve, stop_grace / 4))


async def drain_in_batches(
    bot_ids: Sequence[uuid.UUID],
    drain_one: Callable[[uuid.UUID], Awaitable[None]],
    *,
    deadline: float,
    gap: float = DRAIN_GAP_SECONDS,
    max_batch: int = DRAIN_MAX_BATCH,
) -> list[uuid.UUID]:
    """按批排空，返回到期时还没开始排空的 bot。

    一个 bot 排空抛错只记日志，不拖累同批与后续的 bot（否则剩下的全部落到硬接管）。
    `deadline` 是 `time.monotonic()` 口径。
    """
    pending = list(bot_ids)
    first = True
    while pending:
        now = time.monotonic()
        if now >= deadline:
            break
        if not first and now + gap < deadline:
            await asyncio.sleep(gap)
            now = time.monotonic()
        first = False
        size = batch_size(len(pending), deadline - now, gap=gap, max_batch=max_batch)
        batch, pending = pending[:size], pending[size:]
        results = await asyncio.gather(*(drain_one(b) for b in batch), return_exceptions=True)
        for bot_id, result in zip(batch, results, strict=True):
            if isinstance(result, Exception):
                log.error(
                    "bot_drain_failed",
                    bot_id=str(bot_id),
                    error=type(result).__name__,
                    exc_info=result,
                )
            elif isinstance(result, BaseException):
                raise result
    return pending
