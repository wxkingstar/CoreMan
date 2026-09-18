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
# 带令牌仍跳到这类地址，说明令牌没被认下来（不带令牌时目标可能只回 401，没有可比的跳转）。
LOGIN_HINTS = ("login", "signin")


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


def redirect_target(url: httpx.URL, location: str | None) -> str | None:
    """跳转目标只取主机与路径：查询参数里可能带回调令牌，既不回显也不参与比较。"""
    if not location:
        return None
    try:
        target = url.join(location)
    except httpx.InvalidURL:
        return None
    path = target.path or "/"
    return path if target.host == url.host else f"{target.host}{path}"


async def fetch(
    client: httpx.AsyncClient, url: httpx.URL, token: str | None
) -> tuple[int, str | None]:
    headers = {"Cookie": f"bot_token={token}"} if token else {}
    async with client.stream("GET", url, headers=headers) as response:
        return response.status_code, redirect_target(url, response.headers.get("location"))


def judge(
    subject: str, baseline: tuple[int, str | None], result: tuple[int, str | None]
) -> tuple[bool, str]:
    """对比不带令牌与带令牌两次请求。很多后台登录后首页照样跳转（去默认页或别的子系统），
    所以带令牌的 3xx 只要去向与不带令牌时不同、又不是登录页，就算令牌已被识别。"""
    base_status, base_target = baseline
    status, target = result
    if 200 <= status < 300:
        if 200 <= base_status < 300:
            return False, f"目标首页不带令牌也能打开（HTTP {base_status}），无法判断令牌是否生效"
        return True, f"目标已识别令牌（令牌用户 {subject}）"
    if 300 <= status < 400 and target:
        if target == base_target or any(hint in target.lower() for hint in LOGIN_HINTS):
            return False, (
                f"带令牌仍跳到 {target}，令牌未生效：请检查目标认证配置，"
                f"以及对方是否有用户 {subject} 并已分配权限"
            )
        return True, f"目标已识别令牌，登录后跳转到 {target}（令牌用户 {subject}）"
    return False, f"目标未通过访问测试，请检查目标认证配置，以及对方是否有用户 {subject}"


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
    baseline: tuple[int, str | None] = (0, None)
    result: tuple[int, str | None] = (0, None)
    try:
        async with client:
            baseline = await fetch(client, url, None)
            result = await fetch(client, url, token)
        success, message = judge(subject, baseline, result)
    except httpx.HTTPError:
        success, message = False, "目标连接失败或超时"
    # 只回显跳转目标的主机与路径；不返回 token、页面正文、其余响应头与查询参数：
    # 其中可能包含会话 cookie 和业务敏感内容。
    data = {
        "success": success,
        "status_code": result[0],
        "baseline_status_code": baseline[0],
        "redirect": result[1],
        "url": str(url),
        "user_login": user.login_name,
        "subject": subject,
        "message": message,
    }
    return {"code": 0, "data": data, **data}
