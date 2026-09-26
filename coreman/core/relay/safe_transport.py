"""出站 HTTP 传输：DNS 解析结果固定到本次连接、禁用代理；登记目标另禁重定向。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable

import httpx

_BLOCKED_NAMES = {"localhost", "metadata.google.internal", "instance-data.ec2.internal"}
_BLOCKED_IPS = {"100.100.100.200", "168.63.129.16"}


def allowed_address(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return allowed_address(str(ip.ipv4_mapped))
    return not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or value in _BLOCKED_IPS
    )


def validate_host(host: str) -> None:
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_NAMES or lowered.endswith(".localhost"):
        raise ValueError("目标主机不允许用于运行时调用")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not allowed_address(str(ip)):
        raise ValueError("目标地址不允许用于运行时调用")


def public_address(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return public_address(str(ip.ipv4_mapped))
    return allowed_address(value) and ip.is_global


async def _pinned(
    request: httpx.Request, host: str, port: int, allowed: Callable[[str], bool]
) -> httpx.Request:
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=5,
        )
    except (OSError, TimeoutError) as exc:
        raise httpx.ConnectError("目标主机解析失败", request=request) from exc
    ips = [str(item[4][0]) for item in addresses]
    if not ips or any(not allowed(ip) for ip in ips):
        raise httpx.ConnectError("目标解析到了禁止访问的地址", request=request)
    # URL 使用已经验证过的 IP，不再用域名重新解析；Host 头保留原名称。
    return httpx.Request(
        request.method,
        request.url.copy_with(host=ips[0]),
        headers=request.headers,
        stream=request.stream,
        extensions={**request.extensions, "sni_hostname": host},
    )


class RegisteredTransport(httpx.AsyncBaseTransport):
    """只准向这一个数据库登记的 host:port 发 HTTP，凭证不会跟随 DNS 去内网元数据。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        scheme: str = "http",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_host(host)
        if scheme not in {"http", "https"}:
            raise ValueError("unsupported target scheme")
        self.host, self.port, self.scheme = host.lower(), port, scheme
        self.transport = transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != self.scheme
            or request.url.host != self.host
            or (request.url.port or (443 if self.scheme == "https" else 80)) != self.port
        ):
            raise httpx.ConnectError("请求目标不在实例白名单内", request=request)
        pinned = await _pinned(request, self.host, self.port, allowed_address)
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    """只准访问公网地址：模型给出的 URL 每一跳（含重定向）都重新解析、校验并固定 IP。"""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.scheme not in {"http", "https"} or not host:
            raise httpx.ConnectError("请求目标不允许", request=request)
        try:
            validate_host(host)
        except ValueError as exc:
            raise httpx.ConnectError("请求目标不允许", request=request) from exc
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        pinned = await _pinned(request, host.lower(), port, public_address)
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()
