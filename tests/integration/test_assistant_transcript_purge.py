"""scheduler 每轮清理过期的转录；清理出错不挡后面的步骤。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.assistants import transcripts
from coreman.runtime.scheduler import reaper
from coreman.runtime.scheduler.service import SchedulerService


async def run_until(event: asyncio.Event) -> None:
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        await asyncio.wait_for(event.wait(), 10)
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)


async def test_each_tick_purges_transcripts_older_than_the_session_ttl(
    db_engine: AsyncEngine,
    runtime_settings,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    seen: list[datetime] = []
    called = asyncio.Event()

    async def purge(session: object, before: datetime) -> int:
        seen.append(before)
        called.set()
        return 0

    monkeypatch.setattr(transcripts, "purge", purge)
    await run_until(called)
    # 默认会话 72 小时过期，再多留一天。
    age = datetime.now(seen[0].tzinfo) - seen[0]
    assert timedelta(hours=95) < age < timedelta(hours=97)


async def test_failing_purge_does_not_skip_the_cleanup(
    db_engine: AsyncEngine,
    runtime_settings,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("boom")

    ran = asyncio.Event()

    async def cleanup(*args: object, **kwargs: object) -> dict[str, int]:
        ran.set()
        return {}

    monkeypatch.setattr(transcripts, "purge", broken)
    monkeypatch.setattr(reaper, "run_cleanup", cleanup)
    await run_until(ran)
