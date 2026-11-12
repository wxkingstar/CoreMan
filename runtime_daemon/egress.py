"""助理沙箱的出网放行代理：只接 CONNECT，只放行名单里的「主机:端口」。

每一轮在自己的 Unix socket 上起一个，沙箱里的转发口（sandbox_bridge.py）接到这里。名单由守护
进程按本轮算出：平台地址（控制面地址与本轮 MCP 地址的主机）加上这种 AI 的模型接口地址。登录
令牌的刷新地址不在名单里：沙箱里的 CLI 就算想刷新也连不出去（见 turn_credentials.py）。

不解 TLS、不看内容；日志只记放行与拒绝的次数，不记主机名（主机名本身也可以被拿来夹带内容）。
上游代理沿用节点已有的配置：平台地址走控制面代理，模型接口走 AI 代理，都没配就直连。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOG = logging.getLogger("coreman-runtime")
HEAD_LIMIT = 8 * 1024
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
BUFFER = 64 * 1024
# 各 AI 的模型接口地址（只要推理所需的；OAuth 刷新地址故意不在里面）。节点 config.json 的
# assistant_egress_hosts 可以按 AI 覆盖。
MODEL_HOSTS = {
    "claude": ("api.anthropic.com",),
    "codex": ("chatgpt.com", "api.openai.com"),
}
_CONNECT = re.compile(rb"^CONNECT ([A-Za-z0-9.-]{1,253}):([0-9]{1,5}) HTTP/1\.[01]\r\n")


def endpoint(url: str) -> tuple[str, int] | None:
    """URL 的「主机:端口」；只认 https，端口缺省 443。"""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname:
        return None
    try:
        port = parts.port or 443
    except ValueError:
        return None
    return parts.hostname.lower(), port


@dataclass(frozen=True)
class Policy:
    """本轮的放行名单，以及每个目的地经哪个上游代理（None 为直连）。"""

    allowed: frozenset[tuple[str, int]]
    upstream: Mapping[tuple[str, int], str | None] = field(default_factory=dict)

    def permits(self, host: str, port: int) -> bool:
        return (host.lower(), port) in self.allowed


def turn_policy(
    *,
    provider: str,
    api_url: str,
    env_vars: Mapping[str, str],
    model_hosts: Iterable[str] | None = None,
    control_proxy: str | None = None,
    ai_proxy: str | None = None,
) -> Policy:
    """按本轮算放行名单：控制面地址、本轮所有 `COREMAN_*_URL` 的主机、模型接口地址。"""
    platform: set[tuple[str, int]] = set()
    for url in [api_url, *(v for k, v in env_vars.items() if k.startswith("COREMAN_"))]:
        if not url.startswith("https://"):
            continue
        found = endpoint(url)
        if found is not None:
            platform.add(found)
    models = {(h.lower(), 443) for h in (model_hosts or MODEL_HOSTS.get(provider, ()))}
    upstream: dict[tuple[str, int], str | None] = dict.fromkeys(models, ai_proxy or None)
    upstream.update(dict.fromkeys(platform, control_proxy or None))
    return Policy(frozenset(platform | models), upstream)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if len(head) > HEAD_LIMIT:
        raise ValueError("head too large")
    return head


async def _open(host: str, port: int, proxy: str | None):
    """连目的地；有上游代理时先向它发 CONNECT。"""
    if not proxy:
        return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    parts = urlsplit(proxy)
    if parts.scheme not in ("http", "") or not parts.hostname:
        raise OSError("unsupported upstream proxy")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(parts.hostname, parts.port or 80), CONNECT_TIMEOUT
    )
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if parts.username:
        token = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(token.encode()).decode())
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    status = await _read_head(reader)
    if not re.match(rb"^HTTP/1\.[01] 200", status):
        writer.close()
        raise OSError("upstream proxy refused")
    return reader, writer


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


class Egress:
    """一轮的放行代理。`allowed` / `denied` 只计数。"""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.allowed = 0
        self.denied = 0
        self.server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(path))
        os.chmod(path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 5)
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
        except (  # noqa: UP041 asyncio.TimeoutError is distinct on 3.10
            ValueError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            TimeoutError,
        ):
            writer.close()
            return
        match = _CONNECT.match(head)
        if match is None:
            self.denied += 1
            await self._reply(writer, b"405 Method Not Allowed")
            return
        host, port = match.group(1).decode().lower(), int(match.group(2))
        if not self.policy.permits(host, port):
            self.denied += 1
            await self._reply(writer, b"403 Forbidden")
            return
        try:
            remote_reader, remote_writer = await _open(
                host, port, self.policy.upstream.get((host, port))
            )
        except (  # noqa: UP041 asyncio.TimeoutError is distinct on 3.10
            OSError,
            ValueError,
            asyncio.TimeoutError,
            TimeoutError,
            asyncio.IncompleteReadError,
        ):
            await self._reply(writer, b"502 Bad Gateway")
            return
        self.allowed += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_relay(reader, remote_writer), _relay(remote_reader, writer))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: bytes) -> None:
        with contextlib.suppress(OSError):
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
