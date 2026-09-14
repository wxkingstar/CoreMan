from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coreman.api.errors import ApiError
from coreman.api.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    refresh_session_cookie,
    unsign_session_id,
)
from coreman.core.config import Settings
from coreman.core.db.models import AdminSession, User
from coreman.core.settings_store import SettingsStore

_RENEW_INTERVAL = timedelta(minutes=5)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_store(request: Request) -> SettingsStore:
    return request.app.state.settings_store  # type: ignore[no-any-return]


def client_ip(request: Request) -> str:
    # uvicorn 以 --forwarded-allow-ips 按 Caddy 的 X-Forwarded-For 改写 client.host，
    # 这里不再自行解析
    return request.client.host if request.client else "0.0.0.0"


def user_to_dict(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "login_name": user.login_name,
        "display_name": user.display_name,
        "email": user.email,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "locale": user.locale,
        "source": user.source,
        "team_id": str(user.team_id) if user.team_id else None,
    }


async def current_user(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> User:
    if request.cookies.get("bot_token"):
        from coreman.api.bot_auth import token_user

        return await token_user(request, session)
    unauthorized = ApiError(401, 401, "未登录或会话已过期")
    token = request.cookies.get(SESSION_COOKIE)
    session_id: uuid.UUID | None = (
        unsign_session_id(request.app.state.settings.session_secret, token) if token else None
    )
    if session_id is None:
        raise unauthorized
    row = (
        await session.execute(
            select(AdminSession)
            .where(AdminSession.id == session_id)
            .options(selectinload(AdminSession.user))
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= now
        or row.user.status != "active"
    ):
        raise unauthorized
    if row.last_seen_at is None or now - row.last_seen_at > _RENEW_INTERVAL:
        row.last_seen_at = now
        row.expires_at = now + timedelta(seconds=SESSION_MAX_AGE)
        await session.commit()
        refresh_session_cookie(
            response,
            secret=request.app.state.settings.session_secret,
            session_id=row.id,
            secure=request.app.state.settings.public_base_url.startswith("https://"),
        )
    request.state.admin_session = row
    return row.user
