import asyncio
import base64
import os
import signal

import httpx
import pytest

from coreman.core.config import reset_settings_cache
from coreman.runtime.base import HEALTH_PORTS, Service


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/x")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://t")
    monkeypatch.setenv("MASTER_KEY", base64.b64encode(b"\x09" * 32).decode())
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    reset_settings_cache()


class Probe(Service):
    def __init__(self) -> None:
        super().__init__("worker", port=0, heartbeat_seconds=0.05)
        self.started = False
        self.stopped = False

    async def on_start(self) -> None:
        self.started = True

    async def on_shutdown(self) -> None:
        self.stopped = True


async def _wait_port(svc: Service) -> int:
    for _ in range(100):
        if svc.bound_port:
            return svc.bound_port
        await asyncio.sleep(0.02)
    raise AssertionError("service did not bind")


async def test_health_and_graceful_stop() -> None:
    svc = Probe()
    task = asyncio.create_task(svc.run())
    port = await _wait_port(svc)
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{port}/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["service"] == "worker"
        assert body["instance"].startswith("worker:")
        assert (await c.get(f"http://127.0.0.1:{port}/other")).status_code == 404
    assert svc.started
    svc.request_stop("test")
    await asyncio.wait_for(task, timeout=2)
    assert svc.stopped


async def test_sigterm_stops_service() -> None:
    svc = Probe()
    task = asyncio.create_task(svc.run())
    await _wait_port(svc)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=2)
    assert svc.stopped


def test_ports_table() -> None:
    assert HEALTH_PORTS == {
        "gateway-wecom": 9101,
        "gateway-feishu": 9102,
        "worker": 9103,
        "scheduler": 9104,
    }


async def test_start_failure_closes_listener_and_removes_signal_handlers():
    class Broken(Probe):
        async def on_start(self):
            raise RuntimeError("startup failed")

    svc = Broken()
    with pytest.raises(RuntimeError, match="startup failed"):
        await svc.run()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", svc.bound_port)
    assert not svc.stopped
