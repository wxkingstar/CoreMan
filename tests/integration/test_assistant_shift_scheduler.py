"""看护进程单独一条循环跑冷启动与下班编译：每次都出错也接着跑，不影响别的循环。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from coreman.core.assistants import cold_start, compiling
from coreman.runtime.scheduler import service
from coreman.runtime.scheduler.service import SchedulerService

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_the_scheduler_ticks_cold_start_then_compile_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def cold(_factory: Any, _cipher: Any, now: datetime) -> dict[str, int]:
        order.append("cold_start")
        assert now.tzinfo is not None
        return {"asked": 1}

    async def compile_(_factory: Any, now: datetime) -> dict[str, int]:
        order.append("compile")
        if order.count("compile") > 1:
            raise RuntimeError("synthetic")
        return {"queued": 1}

    monkeypatch.setattr(cold_start, "tick", cold)
    monkeypatch.setattr(compiling, "tick", compile_)
    monkeypatch.setattr(service, "ASSISTANT_SHIFT_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(200):
            if order.count("compile") >= 3:
                break
            await asyncio.sleep(0.05)
        assert svc.is_leader
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert order.count("compile") >= 3
    # 每一轮先冷启动后编译。
    assert order[:4] == ["cold_start", "compile", "cold_start", "compile"]
    assert any(job.get_name() == "scheduler-assistant-shift" for job in svc._bg)
