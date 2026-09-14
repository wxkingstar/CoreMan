"""bot_token 直接认证；不换取比 token 活得更久的普通会话。"""

from __future__ import annotations

import jwt
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.errors import ApiError
from coreman.core.auth.tokens import verify_token
from coreman.core.db.models import User


async def token_user(request: Request, session: AsyncSession) -> User:
    denied = ApiError(401, 401, "机器人令牌无效或已过期")
    raw = request.cookies.get("bot_token")
    if not raw:
        raise denied
    issuer = str(await request.app.state.settings_store.get("jwt_issuer", default="coreman"))
    try:
        claims = await verify_token(session, raw, issuer=issuer, audience="coreman")
    except jwt.InvalidTokenError as exc:
        raise denied from exc
    user = (
        await session.execute(
            select(User).where(
                User.login_name == claims["sub"],
                User.status == "active",
                User.source != "bootstrap",
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise denied
    request.state.bot_token_authenticated = True
    request.state.admin_session = None
    return user
