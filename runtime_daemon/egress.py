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
import ipaddress
import os
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

HEAD_LIMIT = 8 * 1024
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
BUFFER = 64 * 1024
# 单个 Egress 同时最多接多少条连接；超出的直接关掉，免得一轮耗尽守护进程的文件描述符。
MAX_CONNECTIONS = 64
# 已建立的隧道两个方向都这么久没有字节，就把两端关掉。
IDLE_TIMEOUT = 300.0
CLOSE_TIMEOUT = 5.0
# 名单里的平台地址只取平台自己写进环境的这几个键（值是平台的 https 基础地址）。请求级的
# 其他 COREMAN_* 值（发言者姓名、会话号、机器人 env 里自带的键）不可信，不能进名单。
# 与平台保留键一一对应，由 tests/unit/test_assistant_egress.py 对照 env_vars.py 校验。
PLATFORM_URL_KEYS = frozenset(
    {
        "COREMAN_SYSTEMS_MCP_URL",
        "COREMAN_FEISHU_PERSONAL_URL",
        "COREMAN_FEISHU_DOCS_URL",
        "COREMAN_WECOM_PERSONAL_URL",
        "COREMAN_SCHEDULE_URL",
        "COREMAN_CREDENTIAL_URL",
        "COREMAN_CLOUD_PROXY_URL",
    }
)
# 各 AI 的模型接口地址（只要推理所需的；OAuth 刷新地址故意不在里面）。节点 config.json 的
# assistant_egress_hosts 可以按 AI 覆盖。
MODEL_HOSTS = {
    "claude": ("api.anthropic.com",),
    "codex": ("chatgpt.com", "api.openai.com"),
}
_CONNECT = re.compile(rb"^CONNECT ([A-Za-z0-9.-]{1,253}):([0-9]{1,5}) HTTP/1\.[01]\r\n")


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def endpoint(url: str) -> tuple[str, int] | None:
    """URL 的「主机:端口」；只认 https 与域名（地址字面量不收），端口缺省 443。"""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or is_ip_literal(parts.hostname):
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
    """按本轮算放行名单：控制面地址、平台写进环境的地址键（PLATFORM_URL_KEYS）的主机、模型接口地址。"""
    platform: set[tuple[str, int]] = set()
    for url in [api_url, *(v for k, v in env_vars.items() if k in PLATFORM_URL_KEYS)]:
        found = endpoint(url)
        if found is not None:
            platform.add(found)
    models = {
        (h.lower(), 443)
        for h in (model_hosts or MODEL_HOSTS.get(provider, ()))
        if not is_ip_literal(h)
    }
    upstream: dict[tuple[str, int], str | None] = dict.fromkeys(models, ai_proxy or None)
    upstream.update(dict.fromkeys(platform, control_proxy or None))
    return Policy(frozenset(platform | models), upstream)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if len(head) > HEAD_LIMIT:
        raise ValueError("head too large")
    return head


class _Refused(Exception):
    """目的地解析到了回环或链路本地地址：不是平台，也不是模型接口。"""


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _dial(address: str, port: int):
    return await asyncio.open_connection(address, port)


def _forbidden(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified


async def _open(host: str, port: int, proxy: str | None):
    """连目的地；有上游代理时先向它发 CONNECT（解析交给代理），直连时先解析并拒绝回环/链路本地。"""
    if not proxy:
        addresses = await asyncio.wait_for(_resolve(host, port), CONNECT_TIMEOUT)
        if not addresses or any(_forbidden(a) for a in addresses):
            raise _Refused
        last: Exception = OSError("no address")
        for address in addresses:
            try:
                return await asyncio.wait_for(_dial(address, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
                last = exc
        raise last
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


async def _relay(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, activity: list[float]
) -> None:
    """单向转发；两个方向共用 activity，整条隧道 IDLE_TIMEOUT 内没有字节才算空闲。"""
    loop = asyncio.get_running_loop()
    try:
        while True:
            remaining = activity[0] + IDLE_TIMEOUT - loop.time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(reader.read(BUFFER), remaining)
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                continue
            if not data:
                break
            activity[0] = loop.time()
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()


class Egress:
    """一轮的放行代理。`allowed` / `denied` 只计数。"""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.allowed = 0
        self.denied = 0
        self.server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, path: Path) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(path))
        os.chmod(path, 0o600)

    async def close(self) -> None:
        """停止监听并拆掉所有在途连接；返回后没有隧道还活着。"""
        server, self.server = self.server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.transport.abort()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), CLOSE_TIMEOUT)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._tasks) >= MAX_CONNECTIONS:
            self.denied += 1
            with contextlib.suppress(Exception):
                writer.transport.abort()
            return
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 一条连接的故障不能冒到守护进程里
            pass
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
            return
        match = _CONNECT.match(head)
        if match is None:
            self.denied += 1
            await self._reply(writer, b"405 Method Not Allowed")
            return
        host, port = match.group(1).decode().lower(), int(match.group(2))
        if is_ip_literal(host) or not self.policy.permits(host, port):
            self.denied += 1
            await self._reply(writer, b"403 Forbidden")
            return
        try:
            remote_reader, remote_writer = await _open(
                host, port, self.policy.upstream.get((host, port))
            )
        except _Refused:
            self.denied += 1
            await self._reply(writer, b"403 Forbidden")
            return
        except (  # noqa: UP041 asyncio.TimeoutError is distinct on 3.10
            OSError,
            ValueError,
            asyncio.TimeoutError,
            TimeoutError,
            asyncio.IncompleteReadError,
        ):
            await self._reply(writer, b"502 Bad Gateway")
            return
        self._writers.add(remote_writer)
        try:
            self.allowed += 1
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            activity = [asyncio.get_running_loop().time()]
            await asyncio.gather(
                _relay(reader, remote_writer, activity),
                _relay(remote_reader, writer, activity),
            )
        except OSError:
            pass
        finally:
            self._writers.discard(remote_writer)
            with contextlib.suppress(Exception):
                remote_writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: bytes) -> None:
        with contextlib.suppress(OSError):
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
