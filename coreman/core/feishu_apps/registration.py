"""飞书「一键创建智能体应用」注册协议的无状态客户端。

协议与官方 lark-oapi 的 `scene.registration` 一致（OAuth 2.0 Device Authorization Grant）：
init 确认支持 client_secret，begin 拿设备码与确认链接，poll 在用户扫码确认后返回
App ID / App Secret。官方 SDK 在一次调用里阻塞轮询，不适合跨多个 HTTP 请求、多个 API
副本的管理台流程，所以这里拆成 begin / poll 两个独立步骤，状态由调用方加密落库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from coreman.core.feishu_apps import manifest

ACCOUNTS_URL = "https://accounts.feishu.cn"
ENDPOINT = "/oauth/v1/app/registration"
SOURCE = "python-sdk/coreman"
_URL_HOSTS = ("open.feishu.cn", "accounts.feishu.cn")
_MAX_RESPONSE = 256 * 1024
_AVATAR_MAX = 6


class RegistrationError(Exception):
    """脱敏的协议错误；code 可直接给前端做文案映射。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Begin:
    device_code: str
    url: str
    interval: int
    expires_in: int


@dataclass(frozen=True)
class Poll:
    # pending / slow_down / succeeded / denied / expired / lark_unsupported / failed
    status: str
    app_id: str = ""
    app_secret: str = ""
    open_id: str = ""


async def _post(data: dict[str, str], *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    client = http or httpx.AsyncClient(
        base_url=ACCOUNTS_URL,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(15, connect=5),
    )
    try:
        async with client.stream(
            "POST",
            ENDPOINT,
            content=urlencode(data),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            status = response.status_code
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE:
                    raise RegistrationError("upstream_unavailable")
        body = json.loads(raw)
    except (httpx.HTTPError, ValueError) as exc:
        raise RegistrationError("upstream_unavailable") from exc
    finally:
        if http is None:
            await client.aclose()
    # 轮询中的 authorization_pending 等以 4xx 返回，交给调用方按 error 字段判断。
    if not isinstance(body, dict) or status >= 500:
        raise RegistrationError("upstream_unavailable")
    return body


def build_url(
    verification_uri: str,
    *,
    name: str | None = None,
    desc: str | None = None,
    avatars: list[str] | None = None,
    app_id: str | None = None,
) -> str:
    parsed = urlparse(verification_uri)
    if parsed.scheme != "https" or parsed.hostname not in _URL_HOSTS or parsed.username:
        raise RegistrationError("upstream_unavailable")
    params = parse_qs(parsed.query)
    params["from"] = ["sdk"]
    params["tp"] = ["sdk"]
    params["source"] = [SOURCE]
    if avatars:
        params["avatar"] = avatars[:_AVATAR_MAX]
    if name:
        params["name"] = [name]
    if desc:
        params["desc"] = [desc]
    params["addons"] = [manifest.encode_addons(manifest.addons())]
    if app_id:
        # 更新已绑定的应用：确认页只允许补齐该应用的配置。
        params["clientID"] = [app_id]
    else:
        # 新建员工只创建新应用，不提供「选择已有应用」入口。
        params["createOnly"] = ["true"]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


async def begin(
    *,
    name: str | None = None,
    desc: str | None = None,
    avatars: list[str] | None = None,
    app_id: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> Begin:
    init = await _post({"action": "init"}, http=http)
    methods = init.get("supported_auth_methods")
    if not isinstance(methods, list) or "client_secret" not in methods:
        raise RegistrationError("registration_unsupported")
    body = await _post(
        {
            "action": "begin",
            "archetype": "PersonalAgent",
            "auth_method": "client_secret",
            "request_user_info": "open_id",
        },
        http=http,
    )
    device_code = body.get("device_code")
    uri = body.get("verification_uri_complete")
    if not isinstance(device_code, str) or not device_code or not isinstance(uri, str):
        raise RegistrationError("upstream_unavailable")
    try:
        interval = max(1, min(60, int(body.get("interval", 5))))
        expires_in = max(60, min(1800, int(body.get("expires_in", 600))))
    except (TypeError, ValueError) as exc:
        raise RegistrationError("upstream_unavailable") from exc
    url = build_url(uri, name=name, desc=desc, avatars=avatars, app_id=app_id)
    return Begin(device_code, url, interval, expires_in)


async def poll(device_code: str, *, http: httpx.AsyncClient | None = None) -> Poll:
    body = await _post({"action": "poll", "device_code": device_code}, http=http)
    app_id, secret = body.get("client_id"), body.get("client_secret")
    info = body.get("user_info")
    info = info if isinstance(info, dict) else {}
    if isinstance(app_id, str) and app_id and isinstance(secret, str) and secret:
        if info.get("tenant_brand") == "lark":
            return Poll("lark_unsupported")
        open_id = info.get("open_id")
        return Poll("succeeded", app_id, secret, open_id if isinstance(open_id, str) else "")
    if info.get("tenant_brand") == "lark":
        # CoreMan 的网关与接口固定国内飞书域名，国际版 Lark 租户建出来的应用无法连接。
        return Poll("lark_unsupported")
    error = body.get("error")
    if error == "authorization_pending":
        return Poll("pending")
    if error == "slow_down":
        return Poll("slow_down")
    if error == "access_denied":
        return Poll("denied")
    if error == "expired_token":
        return Poll("expired")
    return Poll("failed")
