"""引导管理员登录、当前用户、登出。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session, user_to_dict
from coreman.api.errors import ApiError
from coreman.api.security import (
    DbLoginLimiter,
    _db_ip,
    clear_login_cookies,
    create_admin_session,
    set_login_cookies,
    verify_csrf,
)
from coreman.core.audit import record_audit
from coreman.core.db.models import AdminSession, User
from coreman.core.logging import get_logger

log = get_logger(__name__)
public_router = APIRouter(prefix="/api/auth", tags=["auth"])
# CSRF 挂在路由器级依赖上：/api/admin/* 的写端点一个都不能漏，
# verify_csrf 对 GET/HEAD/OPTIONS 是 no-op，所以只读端点不受影响。
admin_router = APIRouter(
    prefix="/api/admin/auth", tags=["auth"], dependencies=[Depends(verify_csrf)]
)


class BootstrapLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@public_router.post("/bootstrap")
async def bootstrap_login(
    body: BootstrapLogin,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    settings = request.app.state.settings
    ip = client_ip(request)
    limiter = DbLoginLimiter(session)
    if await limiter.is_blocked(ip):
        raise ApiError(429, 429, "登录尝试过于频繁，请 1 分钟后再试")
    if not await request.app.state.settings_store.get("bootstrap_admin_enabled", default=True):
        raise ApiError(403, 403, "引导登录已关闭")
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        raise ApiError(403, 403, "未配置引导管理员")
    # compare_digest 传 str 时只接受 ASCII，非 ASCII 会抛 TypeError（→500，且与 401 构成
    # 用户名是否存在的 oracle）；统一按 UTF-8 编成 bytes 比较。
    ok_user = secrets.compare_digest(
        body.username.encode(), settings.bootstrap_admin_username.encode()
    )
    ok_pass = secrets.compare_digest(
        body.password.encode(), settings.bootstrap_admin_password.encode()
    )
    if not (ok_user and ok_pass):
        log.warning("bootstrap_login_failed", ip=ip)
        await limiter.record_failure(ip)
        raise ApiError(401, 401, "用户名或密码错误")

    # 引导账号只认 source='bootstrap'（users_bootstrap_uk 保证至多一行），不按 login_name
    # 匹配——避免撞上同步来的同名员工、错误地把其提升为 platform_admin。
    user = (
        await session.execute(select(User).where(User.source == "bootstrap"))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            login_name=None,
            display_name=f"引导管理员 ({body.username})",
            role="platform_admin",
            status="active",
            locale="zh",
            source="bootstrap",
        )
        session.add(user)
    else:
        user.role, user.status = "platform_admin", "active"
    await session.flush()
    actor_login = f"bootstrap:{body.username}"
    admin_session = await create_admin_session(
        session,
        user=user,
        auth_method="bootstrap",
        ip=_db_ip(ip),
        user_agent=request.headers.get("user-agent", ""),
    )
    await record_audit(
        session,
        action="auth.bootstrap_login",
        actor_id=user.id,
        actor_login=actor_login,
        target_type="user",
        target_id=str(user.id),
        ip=_db_ip(ip),
    )
    await session.commit()
    set_login_cookies(
        response,
        secret=settings.session_secret,
        session_id=admin_session.id,
        secure=settings.public_base_url.startswith("https://"),
    )
    log.info("bootstrap_login_ok", user=actor_login, ip=ip)
    return {"code": 0, "data": {"user": user_to_dict(user)}}


@admin_router.get("/me")
async def me(user: User = Depends(current_user)) -> dict[str, object]:
    return {"code": 0, "data": user_to_dict(user)}


@admin_router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    row: AdminSession | None = request.state.admin_session
    if row is not None:
        row.revoked_at = datetime.now(UTC)
        session.add(row)
    response.delete_cookie("bot_token", path="/")
    await record_audit(
        session,
        action="auth.logout",
        actor_id=user.id,
        actor_login=user.login_name,
        ip=_db_ip(client_ip(request)),
    )
    await session.commit()
    clear_login_cookies(response)
    return {"code": 0, "data": None}
