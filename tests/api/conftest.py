from __future__ import annotations

import base64
import secrets as _secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.main import create_app
from coreman.api.security import CSRF_COOKIE, SESSION_COOKIE, SESSION_MAX_AGE, sign_session_id
from coreman.core.config import Settings, reset_settings_cache
from coreman.core.db.models import AdminSession, User

MASTER_KEY = base64.b64encode(b"\x07" * 32).decode()


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """让本用例的 app 以 `COREMAN_ENV=prod` 建立（dev 路由整个不注册）。

    只设环境变量不够：`create_app` 读的是 `api_settings` 已经建好的 `Settings` 实例，
    之后再改环境变量对它没有影响，而 fixture 的先后顺序又取决于用例签名。所以真正起
    作用的是 `api_settings` 里那句 `request.fixturenames` 判断——本 fixture 负责把
    进程级的 `get_settings()` 也一并切到 prod，两边口径一致。
    """
    monkeypatch.setenv("COREMAN_ENV", "prod")
    reset_settings_cache()


@pytest.fixture(autouse=True)
def wecom_verified(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """新建企微员工会连企业微信长连接校验凭证；API 用例默认一律放行，并记下校验过的凭证。

    要测校验失败的用例自己再 monkeypatch 一次 `verify.verify_credentials`。
    """
    from coreman.core.wecom_bots import verify

    seen: list[tuple[str, str]] = []

    async def ok(bot_id: str, secret: str, **_: object) -> None:
        seen.append((bot_id, secret))

    monkeypatch.setattr(verify, "verify_credentials", ok)
    return seen


@pytest.fixture
def api_settings(
    request: pytest.FixtureRequest,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,  # type: ignore[no-untyped-def]
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-0123456789")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "pass-1234-bootstrap")
    monkeypatch.setenv("WEB_DIST_DIR", str(tmp_path / "dist-missing"))
    # 默认 dev（多数用例要 dev 注入路由）；用例请求 `prod_env` 时才切 prod。
    monkeypatch.setenv("COREMAN_ENV", "prod" if "prod_env" in request.fixturenames else "dev")
    reset_settings_cache()
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def app(api_settings: Settings, db_engine) -> AsyncIterator[FastAPI]:  # type: ignore[no-untyped-def]
    application = create_app(api_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


async def login_as(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    *,
    role: str = "member",
    team_id: uuid.UUID | None = None,
    login_name: str | None = None,
    display_name: str = "测试用户",
    email: str | None = None,
) -> User:
    """直接造 users + admin_sessions 行并写签名 cookie，供权限矩阵测试以任意角色登录。

    默认带一个唯一邮箱：业务系统令牌的 sub 只认邮箱前缀（见 auth/system_access），
    没有邮箱的账号一个令牌都拿不到。要测「缺邮箱」时显式传 email=None 之后再清空。
    """
    name = login_name or f"u_{_secrets.token_hex(4)}"
    user = User(
        login_name=name,
        display_name=display_name,
        email=email if email is not None else f"{name}@example.test",
        role=role,
        team_id=team_id,
        source="sync",
    )
    db_session.add(user)
    await db_session.flush()
    return await _attach_session(client, db_session, user)


async def _attach_session(client: httpx.AsyncClient, db_session: AsyncSession, user: User) -> User:
    """给已落库的 user 建 admin_sessions 行并写签名 cookie + CSRF 头。"""
    now = datetime.now(UTC)
    row = AdminSession(
        user_id=user.id,
        auth_method="wecom_qr",
        expires_at=now + timedelta(seconds=SESSION_MAX_AGE),
        last_seen_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    csrf = _secrets.token_urlsafe(16)
    client.cookies.set(
        SESSION_COOKIE, sign_session_id("unit-test-session-secret-0123456789", row.id)
    )
    client.cookies.set(CSRF_COOKIE, csrf)
    client.headers["X-CSRF-Token"] = csrf
    return user


async def login_existing(client: httpx.AsyncClient, db_session: AsyncSession, user: User) -> User:
    """以一个已存在的用户行登录（不新建用户），用于「协作者本人再登录」这类用例。"""
    return await _attach_session(client, db_session, user)


@pytest.fixture
async def admin_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """以引导管理员登录并自动带 CSRF 头。"""
    r = await client.post(
        "/api/auth/bootstrap", json={"username": "admin", "password": "pass-1234-bootstrap"}
    )
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = client.cookies["coreman_csrf"]
    return client
