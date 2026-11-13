"""「我的助理」接口用例的公共件：一位登录着的所有者、机器人令牌的替身、错误代码。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.security import CSRF_COOKIE, SESSION_COOKIE, sign_session_id
from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AdminSession, Bot, User
from tests.api.conftest import login_existing
from tests.integration.assistant_helpers import seed_assistant

URL = "/api/me/assistant"


async def owned(
    client: httpx.AsyncClient, db_session: AsyncSession, *, role: str = "member"
) -> tuple[Bot, User]:
    """一位开通好、有数据密钥的助理，并以所有者登录；`role` 是所有者在平台里的角色。"""
    bot, owner = await seed_assistant(db_session)
    owner.role = role
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


def as_bot_token(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, user: User) -> None:
    """让之后的请求以一枚有效的机器人令牌认证成 `user`（没有交互登录会话）。"""
    from coreman.api import bot_auth

    async def token_user(request, session):  # type: ignore[no-untyped-def]
        request.state.bot_token_authenticated = True
        request.state.admin_session = None
        return user

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.set("bot_token", "valid-human-bot-token")


def code(response: httpx.Response) -> str | None:
    """错误信封里的原因代码（`errors[0].type`）。"""
    errors = response.json().get("errors") or [{}]
    found = errors[0].get("type")
    return found if isinstance(found, str) else None


async def fresh(db_session: AsyncSession, *rows: Bot) -> None:
    """丢掉会话里缓存的行，再把这几条助理记录读回来。

    接口在另一个会话里改了库；`expire_all` 之后直接取 `bot.id` 会在同步上下文里触发加载。
    """
    db_session.expire_all()
    for row in rows:
        await db_session.refresh(row)


async def as_cli(client: httpx.AsyncClient, db_session: AsyncSession, user: User) -> None:
    """让之后的请求带着管理命令建的那种会话（`coreman … --as 登录名`）：`auth_method = "cli"`。"""
    now = datetime.now(UTC)
    row = AdminSession(
        user_id=user.id,
        auth_method="cli",
        user_agent="coreman-cli",
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    csrf = secrets.token_urlsafe(16)
    # 与 tests/api/conftest.py 的 _attach_session 用同一把测试密钥。
    client.cookies.set(
        SESSION_COOKIE, sign_session_id("unit-test-session-secret-0123456789", row.id)
    )
    client.cookies.set(CSRF_COOKIE, csrf)
    client.headers["X-CSRF-Token"] = csrf
