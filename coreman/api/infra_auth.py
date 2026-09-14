"""基础设施签名认证。请求的实际路径参与签名，别名不改写路径。"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import ApiClient

INFRA_SCOPES = {"relay", "org", "notify", "push", "systems", "memories", "cron", "escalations"}


async def signed_client(
    request: Request, session: AsyncSession = Depends(get_session)
) -> ApiClient:
    denied = ApiError(401, 401, "接口签名无效或已过期")
    app_key, timestamp, signature = (
        request.headers.get(k, "") for k in ("X-App-Key", "X-Timestamp", "X-Signature")
    )
    if (
        not timestamp.isascii()
        or not timestamp.isdecimal()
        or len(timestamp) > 12
        or abs(time.time() - int(timestamp)) > 600
    ):
        raise denied
    if not app_key or len(app_key) > 128 or len(signature) != 64:
        raise denied
    client = await session.get(ApiClient, app_key)
    if client is None or not client.enabled:
        raise denied
    params: dict[str, Any] = dict(request.query_params)
    if request.method not in ("GET", "HEAD"):
        if len(await request.body()) > 1024 * 1024:
            raise ApiError(413, 413, "请求体过大")
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
            except ValueError as exc:
                raise denied from exc
            if not isinstance(body, dict):
                raise denied
            params.update(body)
        elif (
            "application/x-www-form-urlencoded" in content_type
            or "multipart/form-data" in content_type
        ):
            params.update(dict(await request.form()))
    secret = request.app.state.cipher.decrypt(client.secret_enc, "api_clients.secret_enc")
    try:
        expected = sign_request(
            request.method, request.url.path, params, timestamp, app_key, secret
        )
    except (ValueError, TypeError) as exc:
        raise denied from exc
    if not secrets.compare_digest(signature.encode(), expected.encode()):
        raise denied
    request.state.api_client = client
    return client


def require_scope(scope: str) -> Callable[..., Coroutine[Any, Any, ApiClient]]:
    if scope not in INFRA_SCOPES:
        raise ValueError("unknown infra scope")

    async def dependency(
        client: ApiClient = Depends(signed_client), session: AsyncSession = Depends(get_session)
    ) -> ApiClient:
        if scope not in client.scopes:
            raise ApiError(403, 403, "调用方未获授权访问该接口组")
        # 遥测不抬升客户端配置 version。
        await session.execute(
            update(ApiClient)
            .where(ApiClient.app_key == client.app_key)
            .values(last_used_at=datetime.now(UTC))
        )
        await session.commit()
        return client

    return dependency
