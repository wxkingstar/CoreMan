"""助理沙箱的出网放行代理：名单内的 CONNECT 接通，其余一律拒绝，上游代理照节点配置串上。"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

import pytest

from runtime_daemon import egress


def test_platform_url_keys_match_platform_reserved_keys():
    from coreman.core.prompting.env_vars import RESERVED_KEYS, is_reserved_key

    assert all(is_reserved_key(key) for key in egress.PLATFORM_URL_KEYS)
    assert {k for k in RESERVED_KEYS if k.endswith("_URL")} <= egress.PLATFORM_URL_KEYS
    assert all(key.endswith("_URL") for key in egress.PLATFORM_URL_KEYS)


def test_only_platform_keys_and_domain_names_enter_the_allowlist():
    policy = egress.turn_policy(
        provider="claude",
        api_url="https://coreman.example.com",
        env_vars={
            "COREMAN_USER_NAME": "https://evil.example.com",
            "COREMAN_FOO_URL": "https://foo.example.com",
            "COREMAN_FEISHU_DOCS_URL": "https://169.254.169.254/x",
            "COREMAN_WECOM_PERSONAL_URL": "https://[::1]:8443/x",
            "COREMAN_SCHEDULE_URL": "https://127.0.0.1:9000/x",
        },
        model_hosts=["10.0.0.5", "llm.example.com"],
    )
    assert policy.allowed == {("coreman.example.com", 443), ("llm.example.com", 443)}
    assert egress.endpoint("https://169.254.169.254") is None
    assert egress.endpoint("https://[::1]:8443/") is None


def test_policy_covers_platform_mcp_and_model_hosts_only():
    policy = egress.turn_policy(
        provider="claude",
        api_url="https://coreman.example.com",
        env_vars={
            "COREMAN_FEISHU_PERSONAL_URL": "https://mcp.example.com:8443/api/runtime/x",
            "COREMAN_BOT_KEY": "demo-bot",
            "COREMAN_PLAIN_URL": "http://insecure.example.com/x",
            "OTHER_URL": "https://not-ours.example.com",
        },
        control_proxy="http://control-proxy.example.com:3128",
        ai_proxy="http://ai-proxy.example.com:3128",
    )
    assert policy.allowed == {
        ("coreman.example.com", 443),
        ("mcp.example.com", 8443),
        ("api.anthropic.com", 443),
    }
    assert policy.upstream[("coreman.example.com", 443)] == "http://control-proxy.example.com:3128"
    assert policy.upstream[("api.anthropic.com", 443)] == "http://ai-proxy.example.com:3128"
    assert policy.permits("API.Anthropic.com", 443)
    assert not policy.permits("api.anthropic.com", 80)
    # OAuth 刷新地址不在名单里。
    assert not policy.permits("console.anthropic.com", 443)
    codex = egress.turn_policy(provider="codex", api_url="https://coreman.example.com", env_vars={})
    assert ("chatgpt.com", 443) in codex.allowed and ("auth.openai.com", 443) not in codex.allowed
    custom = egress.turn_policy(
        provider="codex",
        api_url="https://coreman.example.com",
        env_vars={},
        model_hosts=["llm.example.com"],
    )
    assert ("llm.example.com", 443) in custom.allowed and ("chatgpt.com", 443) not in custom.allowed


async def echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        while data := await reader.read(1024):
            writer.write(b"echo:" + data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def socket_path():
    return Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"


@pytest.fixture
def reach(monkeypatch):
    """名单里的名字解析成私网地址，再把拨号重定向到本机的测试服务。"""
    ports: dict[int, int] = {}
    addresses = {"value": "10.0.0.1"}

    async def resolve(host, port):
        return [addresses["value"]]

    async def dial(address, port):
        return await asyncio.open_connection("127.0.0.1", ports.get(port, port))

    monkeypatch.setattr(egress, "_resolve", resolve)
    monkeypatch.setattr(egress, "_dial", dial)
    return ports, addresses


async def request(path: Path, head: bytes, payload: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    body = b""
    if payload and status.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
        body = await asyncio.wait_for(reader.read(1024), 5)
    writer.close()
    return status + body


async def test_allowed_connect_is_tunnelled(socket_path, reach):
    server, port = await echo_server()
    reach[0][443] = port
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n", b"hi")
        assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"echo:hi")
        assert (proxy.allowed, proxy.denied) == (1, 0)
        assert socket_path.stat().st_mode & 0o077 == 0
    finally:
        await proxy.close()
        server.close()


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"CONNECT blocked.example.com:443 HTTP/1.1\r\n\r\n", b"HTTP/1.1 403"),
        (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"HTTP/1.1 403"),
        (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", b"HTTP/1.1 405"),
        (b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", b"HTTP/1.1 405"),
    ],
)
async def test_everything_else_is_refused(socket_path, head, status):
    proxy = egress.Egress(egress.Policy(frozenset({("api.anthropic.com", 443)})))
    await proxy.start(socket_path)
    try:
        assert (await request(socket_path, head)).startswith(status)
        assert (proxy.allowed, proxy.denied) == (0, 1)
    finally:
        await proxy.close()


async def test_upstream_proxy_is_used_with_its_credentials(socket_path):
    seen: list[bytes] = []
    target, port = await echo_server()

    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()

        async def pipe(src, dst):
            while data := await src.read(1024):
                dst.write(data)
                await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

    proxy_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    destination = ("model.example.com", 443)
    proxy = egress.Egress(
        egress.Policy(
            frozenset({destination}),
            {destination: f"http://user:p%40ss@127.0.0.1:{proxy_port}"},
        )
    )
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n", b"x")
        assert out.endswith(b"echo:x")
        assert seen[0].startswith(b"CONNECT model.example.com:443 HTTP/1.1")
        auth = base64.b64encode(b"user:p@ss")
        assert b"Proxy-Authorization: Basic " + auth in seen[0]
    finally:
        await proxy.close()
        proxy_server.close()
        target.close()


async def test_unreachable_destination_is_a_bad_gateway(socket_path, reach):
    reach[0][443] = 1
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 502")
    finally:
        await proxy.close()


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254", "::ffff:127.0.0.1"])
async def test_name_resolving_to_loopback_or_link_local_is_refused(socket_path, reach, address):
    reach[1]["value"] = address
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
        assert (proxy.allowed, proxy.denied) == (0, 1)
    finally:
        await proxy.close()


async def test_name_resolving_to_private_address_is_allowed(socket_path, reach):
    server, port = await echo_server()
    reach[0][443] = port
    reach[1]["value"] = "192.168.1.10"
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n", b"hi")
        assert out.endswith(b"echo:hi")
    finally:
        await proxy.close()
        server.close()


async def test_one_refused_address_among_several_refuses_the_name(socket_path, monkeypatch):
    async def resolve(host, port):
        return ["10.0.0.1", "127.0.0.1"]

    monkeypatch.setattr(egress, "_resolve", resolve)
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    try:
        out = await request(socket_path, b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.close()


async def test_connections_over_the_cap_are_closed_and_counted(socket_path):
    proxy = egress.Egress(egress.Policy(frozenset()))
    await proxy.start(socket_path)
    held = []
    try:
        for _ in range(egress.MAX_CONNECTIONS):
            held.append(await asyncio.open_unix_connection(str(socket_path)))
        await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        assert await asyncio.wait_for(reader.read(10), 5) == b""
        writer.close()
        assert proxy.denied == 1
        for r, _w in held:
            assert not r.at_eof()
    finally:
        for _r, w in held:
            w.close()
        await proxy.close()


async def test_close_tears_down_established_tunnels(socket_path, reach):
    server, port = await echo_server()
    reach[0][443] = port
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n")
    await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.write(b"a")
    assert await asyncio.wait_for(reader.read(10), 5) == b"echo:a"
    await proxy.close()
    try:
        assert await asyncio.wait_for(reader.read(10), 5) == b""
        assert not proxy._tasks and not proxy._writers
    finally:
        writer.close()
        server.close()


async def test_idle_tunnel_is_closed(socket_path, reach, monkeypatch):
    monkeypatch.setattr(egress, "IDLE_TIMEOUT", 0.3)
    server, port = await echo_server()
    reach[0][443] = port
    proxy = egress.Egress(egress.Policy(frozenset({("model.example.com", 443)})))
    await proxy.start(socket_path)
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(b"CONNECT model.example.com:443 HTTP/1.1\r\n\r\n")
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert await asyncio.wait_for(reader.read(10), 5) == b""
    finally:
        writer.close()
        await proxy.close()
        server.close()
