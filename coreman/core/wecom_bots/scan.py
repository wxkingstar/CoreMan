"""企业微信「扫码创建智能机器人」的无状态客户端。

协议来自企业微信官方命令行工具的扫码授权流程，没有公开文档，也没有稳定性保证：

- `GET /ai/qc/generate?source=wecom_cli_external&plat=3` → `{"data": {"scode", "auth_url"}}`，
  `auth_url` 就是二维码内容，其中的 `s=` 参数就是 scode；
- `GET /ai/qc/query_result?scode=…` → `{"data": {"status": "init"}}`，扫码确认后
  状态变为 `success`，`data.bot_info` 带回 `botid` 与 `secret`。

三点直接影响调用方的设计：`query_result` 不需要任何鉴权，谁拿到 scode 谁就能在扫码后取走
Secret，而且成功之后同一个 scode 仍会反复返回同一个 Secret，调用方无法让它失效；接口也不会
告诉你会话过期（未扫码返回 `init`，编造的 scode 返回 `pending`），有效期只能自己计时。
返回结构不符合预期时抛 `unexpected_response`，这通常是接口改版最早的信号。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

BASE_URL = "https://work.weixin.qq.com"
GENERATE = "/ai/qc/generate"
QUERY = "/ai/qc/query_result"
# 接口只认官方命令行工具的来源标识。
SOURCE = "wecom_cli_external"
# 发起端的操作系统类型；CoreMan 服务端运行在 Linux 上。
PLAT = "3"
WAITING = ("init", "pending")
_SCODE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_STATUS = re.compile(r"^[a-z_]{1,32}$")
_MAX_RESPONSE = 64 * 1024


class ScanError(Exception):
    """脱敏的协议错误：upstream_unavailable（网络或服务端故障）或 unexpected_response（改版）。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Generated:
    scode: str
    auth_url: str


@dataclass(frozen=True)
class Query:
    # waiting / succeeded
    status: str
    upstream_status: str
    bot_id: str = ""
    secret: str = ""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, connect=5),
    )


async def _get(
    path: str, params: dict[str, str], *, http: httpx.AsyncClient | None
) -> dict[str, Any]:
    client = http or _client()
    try:
        async with client.stream("GET", path, params=params) as response:
            status = response.status_code
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE:
                    raise ScanError("unexpected_response")
    except httpx.HTTPError as exc:
        raise ScanError("upstream_unavailable") from exc
    finally:
        if http is None:
            await client.aclose()
    if status != 200:
        raise ScanError("upstream_unavailable")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ScanError("unexpected_response") from exc
    if not isinstance(body, dict):
        raise ScanError("unexpected_response")
    errcode = body.get("errcode")
    if errcode not in (None, 0):
        # 限频、风控等业务错误：不是改版，按暂时不可用处理。
        raise ScanError("upstream_unavailable")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ScanError("unexpected_response")
    return data


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


async def generate(*, http: httpx.AsyncClient | None = None) -> Generated:
    data = await _get(GENERATE, {"source": SOURCE, "plat": PLAT}, http=http)
    scode, auth_url = _text(data.get("scode")), _text(data.get("auth_url"))
    if not _SCODE.fullmatch(scode):
        raise ScanError("unexpected_response")
    parsed = urlparse(auth_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "work.weixin.qq.com"
        or parsed.username
        or parse_qs(parsed.query).get("s") != [scode]
    ):
        raise ScanError("unexpected_response")
    return Generated(scode, auth_url)


async def query(scode: str, *, http: httpx.AsyncClient | None = None) -> Query:
    data = await _get(QUERY, {"scode": scode}, http=http)
    status = _text(data.get("status"))
    if not _STATUS.fullmatch(status):
        raise ScanError("unexpected_response")
    info = data.get("bot_info")
    if isinstance(info, dict):
        bot_id = _text(info.get("botid")) or _text(info.get("bot_id"))
        secret = _text(info.get("secret"))
        if bot_id and secret:
            return Query("succeeded", status, bot_id, secret)
    if status in WAITING:
        return Query("waiting", status)
    # 成功却没带凭证，或出现没见过的状态：都可能是改版，交给调用方告警并继续计时。
    raise ScanError("unexpected_response")
