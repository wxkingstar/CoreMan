"""Remote command line: `coreman login` against the app's OAuth server with a real loopback
callback, Bearer calls as the member, silent refresh, logout."""

from __future__ import annotations

import asyncio
import json
import re
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.admin_cli import client as admin_client
from coreman.admin_cli import remote
from tests.api.conftest import login_as


@pytest.fixture
def wired(app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[asyncio.Task[None]]:
    """远程模式的 HTTP 走进程内的应用；配置目录放到临时目录。"""
    monkeypatch.setenv(remote.CONFIG_ENV, str(tmp_path / "cfg"))
    monkeypatch.delenv(admin_client.ACTOR_ENV, raising=False)

    def http(server: str, **kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", **kw
        )

    monkeypatch.setattr(remote, "_http", http)
    return []


def browser(client: httpx.AsyncClient, tasks: list[asyncio.Task[None]], decision: str = "allow"):  # type: ignore[no-untyped-def]
    """代替真浏览器：已登录的成员打开授权页、点按钮，再访问本机回调。"""

    async def drive(url: str) -> None:
        page = await client.get(url)
        ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)
        assert ticket is not None, page.text
        r = await client.post(
            "/api/oauth/authorize", data={"ticket": ticket.group(1), "decision": decision}
        )
        async with httpx.AsyncClient() as real:
            await real.get(r.headers["location"])

    def open_url(url: str) -> bool:
        tasks.append(asyncio.get_running_loop().create_task(drive(url)))
        return True

    return open_url


async def test_login_call_refresh_logout(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    wired: list[asyncio.Task[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await login_as(client, db_session, display_name="张三", role="ai_committee")
    monkeypatch.setattr(remote.webbrowser, "open", browser(client, wired))
    me = await remote.login("http://localhost")
    await asyncio.gather(*wired)
    assert me["id"] == str(user.id)
    path = remote.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["access_token"].startswith("cmo_")

    async with admin_client.connect(None) as c:
        assert (await c.get("/api/admin/auth/me"))["id"] == str(user.id)

    # 访问令牌过期：先续期再调，续期令牌随之轮换。
    creds = remote.load()
    assert creds is not None
    creds.expires_at = 0
    remote.save(creds)
    async with admin_client.connect(None) as c:
        assert (await c.get("/api/admin/auth/me"))["role"] == "ai_committee"
    rotated = remote.load()
    assert rotated is not None and rotated.refresh_token != saved["refresh_token"]

    assert await remote.logout() is True
    assert not path.exists()
    with pytest.raises(admin_client.CliError, match="coreman login"):
        async with admin_client.connect(None):
            pass
    # 服务端的授权也吊销了。
    assert (await client.get("/api/me/oauth-grants")).json()["data"] == []


async def test_denied_login_saves_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    wired: list[asyncio.Task[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await login_as(client, db_session)
    monkeypatch.setattr(remote.webbrowser, "open", browser(client, wired, decision="deny"))
    with pytest.raises(remote.RemoteError, match="授权没有完成"):
        await remote.login("http://localhost")
    await asyncio.gather(*wired)
    assert remote.load() is None


def test_server_address_must_be_https_unless_local() -> None:
    assert (
        remote.normalize_server("https://coreman.example.com/x?y") == "https://coreman.example.com"
    )
    assert remote.normalize_server("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for bad in ("http://coreman.example.com", "ftp://x", "coreman.example.com", "https://u@x"):
        with pytest.raises(remote.RemoteError):
            remote.normalize_server(bad)
