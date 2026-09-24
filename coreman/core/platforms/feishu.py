"""飞书服务端接口：固定官方域名、有界分页、脱敏错误、显式令牌类型。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode

import httpx

BASE_URL = "https://open.feishu.cn"
_SEND_SLOTS: dict[str, float] = {}


class FeishuError(Exception):
    def __init__(self, code: int, reason: str = "API request failed") -> None:
        self.code = code
        super().__init__(f"Feishu {code}: {reason}")


def oauth_url(app_id: str, callback: str, state: str) -> str:
    return (
        BASE_URL
        + "/open-apis/authen/v1/authorize?"
        + urlencode({"app_id": app_id, "redirect_uri": callback, "state": state})
    )


class FeishuClient:
    def __init__(self, app_id: str, secret: str, *, http: httpx.AsyncClient | None = None):
        self.app_id, self._secret = app_id, secret
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
        )
        self._owns_http = http is None
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with self._http.stream(method, path, **kwargs) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024 * 1024:
                        raise FeishuError(-2, "response too large")

                if response.status_code >= 300:
                    # Feishu sends actionable API codes on non-2xx responses too.
                    # Keep only the numeric code, never reflected messages/data.
                    error_code = response.status_code
                    try:
                        error_body = json.loads(raw)
                        api_code = int(error_body.get("code", 0))
                        if api_code > 0:
                            error_code = api_code
                    except (ValueError, TypeError, AttributeError):
                        pass
                    raise FeishuError(error_code, "HTTP failure")
                body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            code = int(body.get("code", -2))
            if code:
                raise FeishuError(code)
            return body
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise FeishuError(-1, "transport or response failure") from exc

    async def get_token(self, *, force: bool = False, kind: str = "tenant") -> str:
        if kind not in {"tenant", "app"}:
            raise ValueError("invalid token kind")
        async with self._token_lock:
            cached = self._tokens.get(kind)
            if cached and not force and cached[1] > time.monotonic():
                return cached[0]
            body = await self._request(
                "POST",
                f"/open-apis/auth/v3/{kind}_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self._secret},
            )
            token = body.get(f"{kind}_access_token")
            if not isinstance(token, str) or not token:
                raise FeishuError(-2, "missing token")
            self._tokens[kind] = (token, time.monotonic() + max(0, int(body.get("expire", 0)) - 60))
            return token

    async def call(
        self, method: str, path: str, *, token: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        explicit = token is not None
        for attempt in range(2):
            auth = token if explicit else await self.get_token(force=bool(attempt))
            try:
                return await self._request(
                    method, path, headers={"Authorization": f"Bearer {auth}"}, **kwargs
                )
            except FeishuError as exc:
                if explicit or attempt or exc.code not in {99991663, 99991668}:
                    raise
        raise AssertionError("unreachable")

    async def user_info_by_code(self, code: str) -> dict[str, Any]:
        body = await self.call(
            "POST",
            "/open-apis/authen/v1/oidc/access_token",
            token=await self.get_token(kind="app"),
            json={"grant_type": "authorization_code", "code": code},
        )
        token = (body.get("data") or {}).get("access_token")
        if not isinstance(token, str) or not token:
            raise FeishuError(-2, "missing user token")
        result = await self.call("GET", "/open-apis/authen/v1/user_info", token=token)
        data = result.get("data")
        if not isinstance(data, dict):
            raise FeishuError(-2, "missing user info")
        return data

    async def media_stream(
        self, message_id: str, file_key: str, *, kind: str = "file"
    ) -> AsyncGenerator[bytes, None]:
        if kind not in {"image", "file"} or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value) for value in (message_id, file_key)
        ):
            raise FeishuError(-2, "invalid resource identifier")
        token = await self.get_token()
        async with self._http.stream(
            "GET",
            f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": kind},
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status_code != 200:
                raise FeishuError(response.status_code, "resource download failed")
            total = 0
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise FeishuError(-2, "resource too large")
                yield chunk

    async def notify_user(
        self, user_id: str, content: str, *, key: str, markdown: bool = False
    ) -> str:
        import hashlib

        # Scheduler 是通知的单领导者；保留余量给同应用的 child（最多约 3 次/秒）。
        now = time.monotonic()
        slot = max(now, _SEND_SLOTS.get(self.app_id, 0) + 0.08)
        _SEND_SLOTS[self.app_id] = slot
        await asyncio.sleep(max(0, slot - now))
        if len(_SEND_SLOTS) > 1024:
            for app in [app for app, deadline in _SEND_SLOTS.items() if deadline < now - 60]:
                _SEND_SLOTS.pop(app, None)
        body = (
            {"zh_cn": {"title": "", "content": [[{"tag": "md", "text": content}]]}}
            if markdown
            else {"text": content}
        )
        result = await self.call(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "user_id"},
            json={
                "receive_id": user_id,
                "msg_type": "post" if markdown else "text",
                "content": json.dumps(body, ensure_ascii=False),
                "uuid": hashlib.sha256(key.encode()).hexdigest()[:32],
            },
        )
        message_id = (result.get("data") or {}).get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise FeishuError(-2, "missing message identifier")
        return message_id

    async def pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token = ""
        for _ in range(2000):
            body = await self.call(
                "GET", path, params={**params, "page_size": 50, "page_token": page_token}
            )
            data = body.get("data")
            if not isinstance(data, dict):
                raise FeishuError(-2, "invalid directory page")
            # 结果为空时飞书省略 items（如没有直属成员的部门只返回 has_more=false）。
            batch = data.get("items", [])
            if not isinstance(batch, list):
                raise FeishuError(-2, "invalid directory page")
            if any(not isinstance(row, dict) for row in batch):
                raise FeishuError(-2, "invalid directory item")
            items.extend(batch)
            if len(items) > 100_000:
                raise FeishuError(-2, "directory limit exceeded")
            if data.get("has_more") is False:
                return items
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                raise FeishuError(-2, "incomplete directory pagination")
            page_token = next_token
            seen.add(page_token)
        raise FeishuError(-2, "directory page limit exceeded")
