"""登记目标的 HTTP 传输；DNS 解析结果固定到本次连接，禁用代理与重定向。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

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
        try:
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    self.host, self.port, type=socket.SOCK_STREAM
                ),
                timeout=5,
            )
        except (OSError, TimeoutError) as exc:
            raise httpx.ConnectError("目标主机解析失败", request=request) from exc
        ips = [str(item[4][0]) for item in addresses]
        if not ips or any(not allowed_address(ip) for ip in ips):
            raise httpx.ConnectError("目标解析到了禁止访问的地址", request=request)
        # URL 使用已经验证过的 IP，不再用域名重新解析；Host 头保留原名称。
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=ips[0]),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.host},
        )
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()
