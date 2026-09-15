"""运行时进程基类：健康服务器、心跳日志、SIGTERM 优雅退出（spec §4.1、§13）。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time

from coreman import __version__
from coreman.core.config import get_settings
from coreman.core.logging import configure_logging, get_logger

HEALTH_PORTS = {"gateway-wecom": 9101, "gateway-feishu": 9102, "worker": 9103, "scheduler": 9104}


class Service:
    def __init__(
        self,
        name: str,
        port: int,
        heartbeat_seconds: float = 30.0,
        log_heartbeat_seconds: float | None = None,
    ) -> None:
        """`heartbeat_seconds` 是子类的业务心跳周期（worker 10 秒写库）；主循环那条「我还活着」
        日志另有节奏，默认跟随它，子类要更慢可以用 `log_heartbeat_seconds` 单独指定。"""
        self.name = name
        self.port = port
        self.heartbeat_seconds = heartbeat_seconds
        self.log_heartbeat_seconds = (
            heartbeat_seconds if log_heartbeat_seconds is None else log_heartbeat_seconds
        )
        self.bound_port = 0
        self.started_at = time.monotonic()
        settings = get_settings()
        base = settings.instance_name or name
        self.instance_id = f"{base}:{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
        self._stop = asyncio.Event()
        self._stop_reason = ""
        self._log = get_logger(f"coreman.runtime.{name}")
        self._level = settings.log_level

    async def on_start(self) -> None:  # 子类重写
        return None

    async def on_shutdown(self) -> None:  # 子类重写
        return None

    def request_stop(self, reason: str = "signal") -> None:
        self._stop_reason = reason
        self._stop.set()

    def _health_payload(self) -> bytes:
        body = {
            "status": "ok",
            "service": self.name,
            "instance": self.instance_id,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "version": __version__,
        }
        return json.dumps(body, ensure_ascii=False).encode()

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
            parts = request_line.decode(errors="replace").split()
            metrics = len(parts) >= 2 and parts[0] == "GET" and parts[1] == "/metrics"
            ok = len(parts) >= 2 and parts[0] == "GET" and parts[1].split("?")[0] == "/health"
            status, body = (
                ("200 OK", self._health_payload()) if ok else ("404 Not Found", b'{"code":404}')
            )
            content_type = "application/json"
            if metrics:
                from coreman.core.observability.metrics import render

                factory = getattr(self, "_factory", None) or getattr(self, "factory", None)
                try:
                    body = await render(factory)
                    status = "200 OK"
                except Exception:
                    status, body = (
                        "503 Service Unavailable",
                        b"coreman_metrics_collection_success 0\n",
                    )
                content_type = "text/plain; version=0.0.4"
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def run(self) -> None:
        configure_logging(service=self.name, instance=self.instance_id, level=self._level)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop, sig.name)
        server = await asyncio.start_server(self._handle_http, host="0.0.0.0", port=self.port)
        self.bound_port = server.sockets[0].getsockname()[1]
        self._log.info("service_started", port=self.bound_port, version=__version__)
        started = False
        try:
            await self.on_start()
            started = True
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.log_heartbeat_seconds)
                except TimeoutError:
                    uptime_s = round(time.monotonic() - self.started_at, 1)
                    self._log.info("heartbeat", uptime_s=uptime_s)
        finally:
            try:
                if started:
                    self._log.info("service_stopping", reason=self._stop_reason)
                    await self.on_shutdown()
            finally:
                server.close()
                await server.wait_closed()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
                self._log.info("service_stopped")
