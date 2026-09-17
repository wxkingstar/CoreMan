"""Revoke personal OAuth tokens without exposing upstream bodies or credentials."""

from __future__ import annotations

import json
from typing import Any

import httpx

# Official larksuite/cli: internal/auth/{device_flow,paths,revoke}.go and core/types.go.
REVOKE_URL = "https://accounts.feishu.cn/oauth/v1/revoke"
MAX_RESPONSE_BYTES = 64 * 1024


async def _revoke_one(
    http: httpx.AsyncClient, app_id: str, secret: str, token: str, hint: str
) -> bool:
    try:
        async with http.stream(
            "POST",
            REVOKE_URL,
            data={
                "client_id": app_id,
                "client_secret": secret,
                "token": token,
                "token_type_hint": hint,
            },
            timeout=20,
            follow_redirects=False,
        ) as response:
            if not 200 <= response.status_code < 300:
                return False
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    return False
                body.extend(chunk)
            if not body:
                return True
            data = json.loads(body)
            return isinstance(data, dict) and not data.get("error") and data.get("code", 0) == 0
    except (httpx.HTTPError, ValueError):
        return False


async def revoke_tokens(
    app_id: str,
    secret: str,
    tokens: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
) -> bool:
    """Attempt every stored token; confirm only when every remote call succeeds.

    Missing credentials or tokens cannot confirm remote revocation. An injected
    client remains caller-owned; production clients ignore environment proxies.
    """
    selected = [
        (hint, tokens[hint])
        for hint in ("access_token", "refresh_token")
        if isinstance(tokens.get(hint), str) and tokens[hint]
    ]
    if not app_id or not secret or not selected:
        return False
    if http is None:
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            return await revoke_tokens(app_id, secret, tokens, http=client)
    successful = True
    for hint, token in selected:
        # Do not short-circuit: failure of one token must not leave the other live.
        revoked = await _revoke_one(http, app_id, secret, token, hint)
        successful = revoked and successful
    return successful
