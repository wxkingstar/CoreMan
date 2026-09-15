"""企微网关整实例排空要落在停机宽限内：bot 多时分批并发，而不是 3 秒一个串行。"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from coreman.runtime.gateway_wecom import service as service_module
from coreman.runtime.gateway_wecom.service import GatewayWecomService


async def test_drain_all_fits_many_bots_into_the_stop_grace(
    runtime_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 按比例缩小：间隔 0.05 秒、宽限 2 秒 ≈ 原来 3 秒 / 120 秒；80 个 bot 串行要 4 秒
    monkeypatch.setattr(service_module, "DRAIN_GAP_SECONDS", 0.05)
    service = GatewayWecomService(port=0, stop_grace_seconds=2.0)
    bots = [uuid.uuid4() for _ in range(80)]
    service.runners = dict.fromkeys(bots)  # type: ignore[arg-type]
    drained: list[uuid.UUID] = []
    active = peak = 0

    async def fake_drain(bot_id: uuid.UUID) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        drained.append(bot_id)
        service.runners.pop(bot_id, None)
        active -= 1

    monkeypatch.setattr(service, "drain_bot", fake_drain)
    try:
        started = time.monotonic()
        await service._drain_all()
        assert sorted(drained) == sorted(bots)
        assert time.monotonic() - started < 2.0
        assert 1 < peak <= 10
    finally:
        await service._engine.dispose()
