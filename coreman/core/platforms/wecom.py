"""企业微信服务端 API 客户端：gettoken 缓存、errcode → WeComError、本期用到的通讯录与登录接口。

文档：获取 access_token /cgi-bin/gettoken；获取部门列表 /cgi-bin/department/list；
获取部门成员详情 /cgi-bin/user/list（需通讯录同步 secret，errcode 48009 = 新增 IP 不可读详情）；
获取访问用户身份 /cgi-bin/auth/getuserinfo；读取成员 /cgi-bin/user/get；
open_userid 转 userid /cgi-bin/batch/openuserid_to_userid。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode

import httpx

from coreman.core.logging import get_logger

log = get_logger(__name__)
BASE_URL = "https://qyapi.weixin.qq.com"
_TOKEN_EXPIRED_CODES = {40014, 42001}
_TOKENS: dict[str, tuple[str, float]] = {}  # cache_key -> (token, expires_at monotonic)


def _body_of(r: httpx.Response) -> dict[str, Any]:
    """把响应归一化为企微 JSON 体；非 2xx 或非 JSON 一律抛 WeComError.

    消息不含 URL，避免 secret 泄漏。

    Args:
        r: httpx 响应对象

    Returns:
        解析后的响应 JSON 对象

    Raises:
        WeComError: 当 HTTP 状态 >= 400 或响应不是 JSON 对象时抛出
    """
    if r.status_code >= 400:
        raise WeComError(-1, f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise WeComError(-2, "响应不是 JSON") from exc
    if not isinstance(body, dict):
        raise WeComError(-2, "响应不是 JSON 对象")
    return body


class WeComError(Exception):
    """企业微信接口错误异常。"""

    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"企微接口错误 {errcode}: {errmsg}")
        self.errcode, self.errmsg = errcode, errmsg


class WeComClient:
    """企业微信服务端 API 客户端。"""

    def __init__(
        self,
        corp_id: str,
        secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        """初始化客户端。

        Args:
            corp_id: 企业 ID
            secret: 应用 secret
            http: 自定义 httpx.AsyncClient（默认创建新客户端）
            base_url: API 基础 URL
        """
        self._corp_id, self._secret = corp_id, secret
        self._http = http or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
            follow_redirects=False,
        )
        self._owns_http = http is None
        self._cache_key = f"{corp_id}:{hashlib.sha256(secret.encode()).hexdigest()[:16]}"

    async def aclose(self) -> None:
        """关闭客户端连接。"""
        if self._owns_http:
            await self._http.aclose()

    async def get_token(self, *, force: bool = False) -> str:
        """获取访问令牌（自动缓存）。

        Args:
            force: 是否强制刷新缓存中的令牌

        Returns:
            访问令牌字符串
        """
        hit = _TOKENS.get(self._cache_key)
        if hit and not force and hit[1] > time.monotonic():
            return hit[0]
        r = await self._http.get(
            "/cgi-bin/gettoken", params={"corpid": self._corp_id, "corpsecret": self._secret}
        )
        data = _body_of(r)
        code = int(data.get("errcode", 0))
        if code != 0:
            raise WeComError(code, str(data.get("errmsg", "")))
        token = str(data["access_token"])
        _TOKENS[self._cache_key] = (
            token,
            time.monotonic() + int(data.get("expires_in", 7200)) - 300,
        )
        return token

    async def _call(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> dict[str, Any]:
        """调用企业微信 API（支持 token 过期自动重试）。

        Args:
            method: HTTP 方法
            path: API 路径
            params: 查询参数
            json: JSON 请求体

        Returns:
            API 响应数据

        Raises:
            WeComError: 当 API 返回错误时抛出
        """
        for attempt in (1, 2):
            token = await self.get_token(force=attempt == 2)
            r = await self._http.request(
                method, path, params={**(params or {}), "access_token": token}, json=json
            )
            body = _body_of(r)
            code = int(body.get("errcode", 0))
            if code in _TOKEN_EXPIRED_CODES and attempt == 1:
                log.info("wecom_token_refresh", errcode=code)
                continue
            if code != 0:
                raise WeComError(code, str(body.get("errmsg", "")))
            return dict(body)
        raise AssertionError("unreachable")

    async def media_stream(self, media_id: str) -> AsyncGenerator[bytes, None]:
        """按块下载应用回调的媒体；令牌过期只重试一次，不跟随重定向。"""
        for attempt in (1, 2):
            token = await self.get_token(force=attempt == 2)
            async with self._http.stream(
                "GET", "/cgi-bin/media/get", params={"access_token": token, "media_id": media_id}
            ) as response:
                response.raise_for_status()
                source = response.aiter_bytes(chunk_size=65536)
                first = await anext(source, b"")
                if (
                    "json" in response.headers.get("content-type", "").lower()
                    and len(first) < 65536
                ):
                    try:
                        value = json.loads(first)
                    except ValueError:
                        value = None
                    if isinstance(value, dict) and value.get("errcode"):
                        code = int(value["errcode"])
                        if code in _TOKEN_EXPIRED_CODES and attempt == 1:
                            continue
                        raise WeComError(code, "媒体下载失败")
                if not first:
                    raise WeComError(-1, "媒体为空")
                yield first
                async for part in source:
                    yield part
                return

    async def send_message(
        self, *, agent_id: int, user_id: str, content: str, msgtype: str = "markdown"
    ) -> dict[str, Any]:
        """发送应用通知；部分无效收件人也按失败处理，不能只检查 errcode。"""
        if (
            msgtype not in {"text", "markdown"}
            or not user_id
            or user_id == "@all"
            or "|" in user_id
        ):
            raise ValueError("invalid notification target or message type")
        body = await self._call(
            "POST",
            "/cgi-bin/message/send",
            json={
                "touser": user_id,
                "agentid": agent_id,
                "msgtype": msgtype,
                msgtype: {"content": content},
                "enable_duplicate_check": 1,
                "duplicate_check_interval": 1800,
            },
        )
        if body.get("invaliduser") or body.get("unlicenseduser"):
            raise WeComError(-3, "接收人不在可用范围或没有许可")
        return body

    async def department_list(self) -> list[dict[str, Any]]:
        """获取部门列表。

        Returns:
            部门信息列表
        """
        return list((await self._call("GET", "/cgi-bin/department/list"))["department"])

    async def user_list(
        self, department_id: int = 1, fetch_child: bool = True
    ) -> list[dict[str, Any]]:
        """获取部门成员列表。

        Args:
            department_id: 部门 ID（默认 1）
            fetch_child: 是否递归获取子部门成员（默认 True）

        Returns:
            成员信息列表
        """
        body = await self._call(
            "GET",
            "/cgi-bin/user/list",
            params={"department_id": department_id, "fetch_child": 1 if fetch_child else 0},
        )
        return list(body.get("userlist", []))

    async def user_info_by_code(self, code: str) -> dict[str, Any]:
        """通过登录代码获取用户身份。

        Args:
            code: 企业微信登录代码

        Returns:
            用户身份信息
        """
        return await self._call("GET", "/cgi-bin/auth/getuserinfo", params={"code": code})

    async def user_get(self, userid: str) -> dict[str, Any]:
        """获取成员详细信息。

        Args:
            userid: 成员 ID

        Returns:
            成员详细信息
        """
        return await self._call("GET", "/cgi-bin/user/get", params={"userid": userid})

    async def openuserid_to_userid(self, open_userids: list[str]) -> dict[str, str]:
        """密文 open_userid → 明文 userid（自建应用 access_token；成员须在应用可见范围内）。

        Args:
            open_userids: 待转换的 open_userid 列表

        Returns:
            open_userid → userid；`invalid_open_userid_list` 里的不出现在结果里
        """
        if not open_userids:
            return {}
        body = await self._call(
            "POST", "/cgi-bin/batch/openuserid_to_userid", json={"open_userid_list": open_userids}
        )
        out: dict[str, str] = {}
        for item in body.get("userid_list") or []:
            if isinstance(item, dict) and item.get("open_userid") and item.get("userid"):
                out[str(item["open_userid"])] = str(item["userid"])
        return out


def qr_login_url(corp_id: str, agent_id: str, redirect_uri: str, state: str) -> str:
    """生成企业微信二维码登录链接。

    Args:
        corp_id: 企业 ID
        agent_id: 应用 ID
        redirect_uri: 重定向 URI
        state: 状态参数

    Returns:
        二维码登录链接
    """
    q = urlencode(
        {
            "login_type": "CorpApp",
            "appid": corp_id,
            "agentid": agent_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://login.work.weixin.qq.com/wwlogin/sso/login?{q}"


def oauth_url(corp_id: str, agent_id: str, redirect_uri: str, state: str) -> str:
    """生成企业微信 OAuth 授权链接。

    Args:
        corp_id: 企业 ID
        agent_id: 应用 ID
        redirect_uri: 重定向 URI
        state: 状态参数

    Returns:
        OAuth 授权链接
    """
    q = urlencode(
        {
            "appid": corp_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
            "agentid": agent_id,
        }
    )
    return f"https://open.weixin.qq.com/connect/oauth2/authorize?{q}#wechat_redirect"
