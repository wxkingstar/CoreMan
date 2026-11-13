"""看护进程单独一条循环盯消息：每次都出错也接着跑，不影响别的循环。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from coreman.core.assistants import watch
from coreman.runtime.scheduler import service
from coreman.runtime.scheduler.service import SchedulerService

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_the_scheduler_ticks_the_watch_in_its_own_loop_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[datetime] = []

    async def broken(_factory: Any, _cipher: Any, now: datetime) -> dict[str, int]:
        calls.append(now)
        if len(calls) == 1:
            return {"assistant_watch_polled": 1}
        raise RuntimeError("synthetic")

    monkeypatch.setattr(watch, "tick", broken)
    monkeypatch.setattr(service, "ASSISTANT_WATCH_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.05)
        assert svc.is_leader
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(calls) >= 3 and all(now.tzinfo is not None for now in calls)
    assert any(job.get_name() == "scheduler-assistant-watch" for job in svc._bg)
