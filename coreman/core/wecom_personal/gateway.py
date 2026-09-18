"""企业微信智能机器人「可使用权限」网关的无状态客户端。

开放文档（智能机器人 → AI 能力）只写了成员在机器人编辑页授权、再复制 MCP 链接的用法；程序化
接入的协议取自企业微信官方命令行工具 wecom-cli（github.com/WecomTeam/wecom-cli，MIT）：

- `POST /cgi-bin/aibot/cli/get_cli_config`，请求体 `{bot_id, time, nonce, signature,
  bind_source}`，`signature = sha256_hex(secret + bot_id + time + nonce)`，返回 `{errcode, token}`；
- 业务方法 `POST https://qyapi.weixin.qq.com/cli<path>`，带 `Authorization: Bearer <token>`，
  请求体 `{"payload": "<参数 JSON 字符串>"}`；返回 `{errcode, errmsg, results_json}`，
  `results_json` 解开是 `{result: "<结果 JSON 字符串>", error: {code, message}}`；
- errcode 853004 / 853005 表示令牌过期或无效，重新签名换一个即可。

令牌是机器人级的，调用里不带发言人：企业微信按「授权真人用户」执行，`/identity/whoami` 能查到
这个人是谁。错误里绝不带令牌、Secret 或请求 URL。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from coreman import __version__

AUTH_URL = "https://qyapi.weixin.qq.com/cgi-bin/aibot/cli/get_cli_config"
BASE_URL = "https://qyapi.weixin.qq.com/cli"
# 853004 令牌过期、853005 令牌无效（例如机器人重置了 Secret）：都重新签名换一个。
TOKEN_ERRORS = frozenset({853004, 853005})
# 与官方工具的「手动输入凭证」来源一致；扫码来源只在工具自己扫码创建机器人时使用。
BIND_SOURCE = 1
MAX_RESPONSE_BYTES = 8_000_000
CLIENT_INFO = json.dumps(
    {"platform": "linux", "version": __version__, "distribution": "coreman"},
    separators=(",", ":"),
)
# whoami 的身份说明形如「机器人身份：\n名字：…\nID：…\n授权真人用户身份：\n名字：…\nID：…」。
_ID_AFTER_NAME = r"[:：]\s*名字[:：][^\n]*\n\s*ID[:：]\s*([A-Za-z0-9_@.-]{1,128})"
_AUTHORIZER = re.compile("授权真人用户身份" + _ID_AFTER_NAME)
_BOT = re.compile("机器人身份" + _ID_AFTER_NAME)


class GatewayError(Exception):
    """脱敏的网关错误。

    code：upstream_unavailable（网络、HTTP 或结构异常）、credentials_rejected（换令牌被拒）、
    token_expired（853004 / 853005）、wecom_error（企业微信业务错误，带 errcode 与截断后的说明）。
    """

    def __init__(self, code: str, errcode: int | None = None, message: str = "") -> None:
        self.code, self.errcode, self.message = code, errcode, message
        super().__init__(code if errcode is None else f"{code}:{errcode}")


@dataclass(frozen=True)
class Identity:
    bot_id: str
    # 没有人授权过这个机器人时为空。
    authorizer_id: str | None


def sign(secret: str, bot_id: str, at: int, nonce: str) -> str:
    return hashlib.sha256(f"{secret}{bot_id}{at}{nonce}".encode()).hexdigest()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=httpx.Timeout(20, connect=5)
    )


def _errcode(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _clip(message: Any, *secrets_: str) -> str:
    text = str(message or "")[:200]
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


async def _post(
    url: str, body: dict[str, Any], headers: dict[str, str], http: httpx.AsyncClient | None
) -> dict[str, Any]:
    client = http or _client()
    try:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            status = response.status_code
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise GatewayError("upstream_unavailable")
    except httpx.HTTPError as exc:
        raise GatewayError("upstream_unavailable") from exc
    finally:
        if http is None:
            await client.aclose()
    if status != 200:
        raise GatewayError("upstream_unavailable")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GatewayError("upstream_unavailable") from exc
    if not isinstance(data, dict):
        raise GatewayError("upstream_unavailable")
    return data


async def fetch_token(bot_id: str, secret: str, *, http: httpx.AsyncClient | None = None) -> str:
    """用机器人凭证换网关令牌。凭证不对或机器人没开 API 模式时企业微信返回非 0 errcode。"""
    if not bot_id or not secret:
        raise GatewayError("credentials_rejected")
    at = int(time.time())
    nonce = f"coreman_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
    data = await _post(
        AUTH_URL,
        {
            "bot_id": bot_id,
            "time": at,
            "nonce": nonce,
            "signature": sign(secret, bot_id, at, nonce),
            "bind_source": BIND_SOURCE,
        },
        {"x-wecom-cli-info": CLIENT_INFO},
        http,
    )
    errcode = _errcode(data.get("errcode", 0))
    if errcode != 0:
        raise GatewayError("credentials_rejected", errcode, _clip(data.get("errmsg"), secret))
    token = data.get("token")
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise GatewayError("upstream_unavailable")
    return token


def _raise_for(errcode: int, message: Any, token: str) -> None:
    if errcode in TOKEN_ERRORS:
        raise GatewayError("token_expired", errcode)
    raise GatewayError("wecom_error", errcode, _clip(message, token))


async def invoke(
    token: str, path: str, payload: dict[str, Any], *, http: httpx.AsyncClient | None = None
) -> Any:
    """调一个网关方法，返回解开后的结果。长任务（返回 taskid）由调用方自己决定是否支持。"""
    data = await _post(
        BASE_URL + path,
        {"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        {"Authorization": "Bearer " + token, "x-wecom-cli-info": CLIENT_INFO},
        http,
    )
    errcode = _errcode(data.get("errcode", 0))
    if errcode != 0:
        _raise_for(errcode, data.get("errmsg"), token)
    try:
        inner = json.loads(data["results_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayError("upstream_unavailable") from exc
    if not isinstance(inner, dict):
        raise GatewayError("upstream_unavailable")
    error = inner.get("error")
    if isinstance(error, dict) and _errcode(error.get("code", 0)) != 0:
        _raise_for(_errcode(error.get("code")), error.get("message"), token)
    if inner.get("taskid"):
        return {"taskid": str(inner["taskid"])}
    result = inner.get("result")
    if result is None:
        return {}
    if not isinstance(result, str):
        raise GatewayError("upstream_unavailable")
    try:
        return json.loads(result) if result else {}
    except ValueError as exc:
        raise GatewayError("upstream_unavailable") from exc


def parse_identity(context: str) -> Identity | None:
    """从 whoami 的身份说明里取出机器人 ID 与授权人 ID；格式认不出来返回 None。"""
    bot = _BOT.search(context)
    if bot is None:
        return None
    person = _AUTHORIZER.search(context)
    return Identity(bot.group(1), person.group(1) if person else None)


async def whoami(token: str, *, http: httpx.AsyncClient | None = None) -> Identity:
    result = await invoke(token, "/identity/whoami", {}, http=http)
    context = result.get("extra_identity_context") if isinstance(result, dict) else None
    identity = parse_identity(context) if isinstance(context, str) else None
    if identity is None:
        # 身份说明是给模型看的自然语言，改版最先坏在这里：宁可拒绝，也不猜授权人。
        raise GatewayError("upstream_unavailable")
    return identity


async def discovery(token: str, service: str, *, http: httpx.AsyncClient | None = None) -> Any:
    return await invoke(token, "/service/discovery", {"service": service}, http=http)
