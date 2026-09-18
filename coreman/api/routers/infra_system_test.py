"""授权连通性测试始终使用当前管理员身份，签名调用也须提供经过验证的用户会话。"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.infra_auth import require_scope
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.auth.system_access import RESERVED_SYSTEM_KEYS, token_subject
from coreman.core.auth.tokens import issue_token, signing_key
from coreman.core.db.models import BusinessSystem, User
from coreman.core.relay.safe_transport import RegisteredTransport

router = APIRouter(tags=["system-test"], dependencies=[Depends(verify_csrf)])
MANAGERS = require_roles("ai_committee", "platform_admin")


class TestAccessIn(BaseModel):
    system_key: str = Field(min_length=1, max_length=50)
    email_prefix: str | None = Field(default=None, max_length=100)
    user_name: str | None = Field(default=None, max_length=100)


def make_http(url: httpx.URL) -> httpx.AsyncClient:
    if url.username or url.password or url.fragment or not url.host:
        raise ValueError("invalid target URL")
    return httpx.AsyncClient(
        transport=RegisteredTransport(
            url.host, url.port or (443 if url.scheme == "https" else 80), scheme=url.scheme
        ),
        follow_redirects=False,
        trust_env=False,
        timeout=10,
    )


@router.post("/api/admin/systems/test-access")
@router.post("/api/infra/systems/test-access", dependencies=[Depends(require_scope("systems"))])
async def test_access(
    body: TestAccessIn,
    request: Request,
    user: User = Depends(MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if user.source == "bootstrap" or not user.login_name:
        raise ApiError(403, 403, "请使用已绑定的真实用户测试")
    subject = await token_subject(session, user)
    if (body.email_prefix and body.email_prefix not in (user.login_name, subject)) or (
        body.user_name and body.user_name != user.display_name
    ):
        raise ApiError(403, 403, "不能为其他用户签发测试令牌")
    system = await session.get(BusinessSystem, body.system_key)
    if system is None or not system.enabled:
        raise not_found("业务系统不存在或未启用")
    # 历史库里的保留 key：签出的令牌能直接调用 CoreMan 管理 API，不发给外部地址。
    if system.key in RESERVED_SYSTEM_KEYS:
        raise ApiError(422, 422, "该系统标识为平台保留，不能签发测试令牌")
    if not system.base_url:
        raise ApiError(422, 422, "业务系统尚未配置地址")
    try:
        url = httpx.URL(system.base_url)
        client = make_http(url)
    except ValueError as exc:
        raise ApiError(422, 422, "业务系统地址不允许访问") from exc
    cipher = request.app.state.cipher
    key = await signing_key(session, cipher, request.app.state.settings.external_jwt_key)
    issuer = str(await request.app.state.settings_store.get("jwt_issuer", default="coreman"))
    token = issue_token(
        key,
        cipher,
        issuer=issuer,
        login=subject,
        name=user.display_name,
        audience=system.key,
        ttl=60,
    )
    await record_audit(
        session,
        action="system.test_access",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="system",
        target_id=system.key,
        ip=client_ip(request),
    )
    await session.commit()
    status = 0
    try:
        async with client:
            async with client.stream(
                "GET", url, headers={"Cookie": f"bot_token={token}"}
            ) as response:
                status = response.status_code
        message = (
            f"目标已响应（令牌用户 {subject}）"
            if 200 <= status < 300
            else f"目标未通过访问测试，请检查目标认证配置，以及对方是否有用户 {subject}"
        )
    except httpx.HTTPError:
        message = "目标连接失败或超时"
    # 不返回 token、页面正文或响应头：其中可能包含会话 cookie 和业务敏感内容。
    data = {
        "success": 200 <= status < 300,
        "status_code": status,
        "url": str(url),
        "user_login": user.login_name,
        "subject": subject,
        "message": message,
    }
    return {"code": 0, "data": data, **data}
