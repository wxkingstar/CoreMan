"""远程模式：成员在自己电脑上用 `coreman login` 登录，之后的管理命令经 HTTP 以本人身份调管理 API。

登录走 CoreMan 自己的 OAuth 授权服务器（授权码 + PKCE，回调到本机回环地址），scope 为 `api`：
权限、审计与在管理台里操作一致。令牌存在本机配置目录（权限 0600），访问令牌过期前自动续期。
见 docs/superpowers/specs/2026-11-25-coreman-oauth-mcp-cli.md。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

CONFIG_ENV = "COREMAN_CONFIG_DIR"
SERVER_ENV = "COREMAN_SERVER"
CLIENT_NAME = "CoreMan 命令行"
LOGIN_TIMEOUT = 300
TIMEOUT = 300
# 访问令牌离过期不到这么久就先续期，免得命令跑到一半 401。
REFRESH_MARGIN = 60


class RemoteError(Exception):
    """远程模式的错误：消息原样给用户看。"""


@dataclass
class Credentials:
    server: str
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float


def config_path() -> Path:
    base = os.environ.get(CONFIG_ENV) or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "coreman"
    )
    return Path(base) / "credentials.json"


def load() -> Credentials | None:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Credentials(**{k: data[k] for k in Credentials.__dataclass_fields__})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"本机登录信息损坏（{path}），请重新 coreman login：{exc}") from None


def save(creds: Credentials) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    # 先建成 0600 再写内容：令牌不会有一刻对别的用户可读。
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(creds), fh)
    os.replace(tmp, path)


def forget() -> None:
    with contextlib.suppress(FileNotFoundError):
        config_path().unlink()


def normalize_server(server: str) -> str:
    """服务器地址只留协议和主机；非本机地址必须是 https，令牌不能走明文。"""
    parts = urlsplit(server.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username:
        raise RemoteError(f"服务器地址不对：{server}（例如 https://coreman.example.com）")
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise RemoteError("只有本机地址可以用 http，其余请用 https")
    return f"{parts.scheme}://{parts.netloc}"


def _http(server: str, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=server,
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": "coreman-cli"},
        **kw,
    )


async def _metadata(http: httpx.AsyncClient) -> dict[str, Any]:
    r = await http.get("/.well-known/oauth-authorization-server")
    if r.status_code != 200:
        raise RemoteError(f"这个地址不是支持远程登录的 CoreMan（{r.status_code}）")
    data: dict[str, Any] = r.json()
    return data


def _oauth_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        return str(body.get("error_description") or body.get("error") or r.text)
    except ValueError:
        return r.text or f"HTTP {r.status_code}"


async def _wait_for_code(state: str) -> tuple[asyncio.Server, int, asyncio.Future[dict[str, str]]]:
    """在本机回环地址上等授权回调；只认 /callback 且 state 对得上的那一次。"""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, str]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            target = line.split(" ")[1] if line.count(" ") >= 2 else ""
            parts = urlsplit(target)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            ok = parts.path == "/callback" and query.get("state") == state
            if ok and not result.done():
                result.set_result(query)
            message = "登录完成，可以关闭这个页面回到终端了。" if ok else "无效的回调。"
            body = f"<!doctype html><meta charset='utf-8'><p>{message}</p>".encode()
            writer.write(
                b"HTTP/1.1 "
                + (b"200 OK" if ok else b"400 Bad Request")
                + b"\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, result


async def login(server: str, *, open_browser: bool = True) -> dict[str, Any]:
    """完整的登录流程，返回当前成员信息。"""
    server = normalize_server(server)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    callback_server, port, waiting = await _wait_for_code(state)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    try:
        async with _http(server) as http:
            meta = await _metadata(http)
            r = await http.post(
                meta["registration_endpoint"],
                json={
                    "client_name": CLIENT_NAME,
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            if r.status_code != 201:
                raise RemoteError(f"注册客户端失败：{_oauth_error(r)}")
            client_id = str(r.json()["client_id"])
            url = (
                meta["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": "api",
                        "state": state,
                        "code_challenge": challenge.decode(),
                        "code_challenge_method": "S256",
                    }
                )
            )
            print(f"在浏览器里打开下面的地址登录并确认授权：\n\n  {url}\n", file=sys.stderr)
            if open_browser:
                with contextlib.suppress(webbrowser.Error):
                    webbrowser.open(url)
            try:
                query = await asyncio.wait_for(waiting, LOGIN_TIMEOUT)
            except TimeoutError:
                raise RemoteError("等待浏览器授权超时，请重新 coreman login") from None
            if "error" in query:
                raise RemoteError(
                    f"授权没有完成：{query.get('error_description') or query['error']}"
                )
            if query.get("iss") not in (None, meta.get("issuer")):
                raise RemoteError("回调来自别的授权服务器，已拒绝")
            r = await http.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": query.get("code", ""),
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            if r.status_code != 200:
                raise RemoteError(f"换取令牌失败：{_oauth_error(r)}")
            tokens = r.json()
            creds = Credentials(
                server=server,
                client_id=client_id,
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                expires_at=time.time() + int(tokens.get("expires_in", 3600)),
            )
            save(creds)
            me = await http.get(
                "/api/admin/auth/me", headers={"Authorization": f"Bearer {creds.access_token}"}
            )
            data: dict[str, Any] = me.json().get("data") or {}
            return data
    finally:
        callback_server.close()


async def refresh(creds: Credentials) -> Credentials:
    async with _http(creds.server) as http:
        r = await http.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id,
            },
        )
    if r.status_code != 200:
        raise RemoteError(f"登录已失效（{_oauth_error(r)}），请重新 coreman login")
    tokens = r.json()
    creds.access_token = tokens["access_token"]
    creds.refresh_token = tokens["refresh_token"]
    creds.expires_at = time.time() + int(tokens.get("expires_in", 3600))
    save(creds)
    return creds


async def logout() -> bool:
    """吊销本机的授权并删掉登录信息；没登录过返回 False。"""
    creds = load()
    if creds is None:
        return False
    with contextlib.suppress(httpx.HTTPError):
        async with _http(creds.server) as http:
            await http.post(
                "/api/oauth/revoke",
                data={"token": creds.refresh_token, "client_id": creds.client_id},
            )
    forget()
    return True


class _Bearer(httpx.Auth):
    """每个请求带上访问令牌；快过期先续期，401 时续期后重发一次。"""

    def __init__(self, creds: Credentials) -> None:
        self.creds = creds

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.creds.expires_at - REFRESH_MARGIN < time.time():
            self.creds = await refresh(self.creds)
        request.headers["Authorization"] = f"Bearer {self.creds.access_token}"
        response = yield request
        if response.status_code == 401:
            self.creds = await refresh(self.creds)
            request.headers["Authorization"] = f"Bearer {self.creds.access_token}"
            yield request


@contextlib.asynccontextmanager
async def session(creds: Credentials) -> AsyncIterator[httpx.AsyncClient]:
    async with _http(creds.server, auth=_Bearer(creds)) as http:
        yield http
