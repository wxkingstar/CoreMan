"""网关整实例排空的节奏：分批错开、批内有界并发，总时长落在宽限内。"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from coreman.runtime.gateway_common import drain as pacing


@pytest.mark.parametrize(
    ("remaining", "time_left", "expected"),
    [
        (5, 100.0, 1),  # bot 少：与原来的串行节奏一致
        (33, 100.0, 1),
        (40, 100.0, 2),  # 原来 120 秒宽限的临界规模
        (100, 99.0, 4),
        (1000, 99.0, 10),  # 单批封顶
        (7, 1.0, 7),  # 只剩最后一批的时间：剩下的一次排
        (0, 50.0, 0),
    ],
)
def test_batch_size_fits_remaining_bots_into_remaining_rounds(
    remaining: int, time_left: float, expected: int
) -> None:
    assert pacing.batch_size(remaining, time_left, gap=3.0, max_batch=10) == expected


def test_shutdown_budget_keeps_a_reserve_for_cleanup() -> None:
    assert pacing.shutdown_budget(120) == 110
    assert pacing.shutdown_budget(10) == 7.5
    assert pacing.shutdown_budget(0) == 0


class Recorder:
    def __init__(self, work: float = 0.0, fail: set[uuid.UUID] | None = None) -> None:
        self.work, self.fail = work, fail or set()
        self.active = self.peak = 0
        self.drained: list[uuid.UUID] = []

    async def __call__(self, bot_id: uuid.UUID) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.work)
            if bot_id in self.fail:
                raise RuntimeError("stream push failed")
            self.drained.append(bot_id)
        finally:
            self.active -= 1


async def test_many_bots_drain_within_the_deadline_with_bounded_concurrency() -> None:
    bots = [uuid.uuid4() for _ in range(60)]
    recorder = Recorder(work=0.01)
    started = time.monotonic()
    # 间隔 0.05 秒、预算 1 秒 ≈ 20 批：串行要 3 秒，必须并发才排得完
    left = await pacing.drain_in_batches(
        bots, recorder, deadline=started + 1.0, gap=0.05, max_batch=10
    )
    assert left == []
    assert sorted(recorder.drained) == sorted(bots)
    assert time.monotonic() - started < 1.0
    assert 1 < recorder.peak <= 10


async def test_few_bots_keep_the_serial_gap() -> None:
    bots = [uuid.uuid4() for _ in range(3)]
    recorder = Recorder()
    started = time.monotonic()
    left = await pacing.drain_in_batches(bots, recorder, deadline=started + 10, gap=0.1)
    assert left == [] and recorder.peak == 1
    assert time.monotonic() - started >= 0.2  # 3 个 bot 之间两次间隔


async def test_expired_deadline_returns_the_bots_left_undrained() -> None:
    bots = [uuid.uuid4() for _ in range(3)]
    recorder = Recorder()
    left = await pacing.drain_in_batches(bots, recorder, deadline=time.monotonic() - 1)
    assert left == bots and recorder.drained == []


async def test_one_failing_bot_does_not_stop_the_rest() -> None:
    bots = [uuid.uuid4() for _ in range(4)]
    recorder = Recorder(fail={bots[1]})
    left = await pacing.drain_in_batches(bots, recorder, deadline=time.monotonic() + 5, gap=0.01)
    assert left == []
    assert recorder.drained == [bots[0], *bots[2:]]
