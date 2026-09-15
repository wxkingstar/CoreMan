import itertools
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, unsign_session_id
from coreman.core.db.models import AdminSession, AuditLog, User


async def _login(
    client: httpx.AsyncClient, password: str = "pass-1234-bootstrap"
) -> httpx.Response:
    return await client.post(
        "/api/auth/bootstrap", json={"username": "admin", "password": password}
    )


async def test_bootstrap_login_success(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    r = await _login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["user"]["login_name"] is None
    assert body["data"]["user"]["source"] == "bootstrap"
    assert body["data"]["user"]["role"] == "platform_admin"
    assert SESSION_COOKIE in r.cookies and CSRF_COOKIE in r.cookies
    user = (await db_session.execute(select(User))).scalar_one()
    assert user.role == "platform_admin" and user.status == "active" and user.source == "bootstrap"
    audit = (await db_session.execute(select(AuditLog))).scalar_one()
    assert audit.action == "auth.bootstrap_login" and audit.actor_login == "bootstrap:admin"
    # 迁移 0002 的 admin_sessions_auth_method_check 依赖这个取值
    assert (await db_session.execute(select(AdminSession))).scalar_one().auth_method == "bootstrap"


async def test_bootstrap_never_touches_synced_user_named_admin(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        User(login_name="admin", display_name="同步来的 admin", role="member", status="disabled")
    )
    await db_session.commit()
    assert (await _login(client)).status_code == 200
    rows = (await db_session.execute(select(User).order_by(User.created_at))).scalars().all()
    assert len(rows) == 2
    synced = next(r for r in rows if r.login_name == "admin")
    assert synced.role == "member" and synced.status == "disabled"
    assert next(r for r in rows if r.source == "bootstrap").login_name is None


async def test_bootstrap_login_reuses_single_bootstrap_user(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    assert (await _login(client)).status_code == 200
    assert (await _login(client)).status_code == 200
    assert len((await db_session.execute(select(User))).scalars().all()) == 1


async def test_wrong_password_401(client: httpx.AsyncClient) -> None:
    r = await _login(client, password="nope")
    assert r.status_code == 401
    assert r.json() == {"code": 401, "message": "用户名或密码错误"}


async def test_non_ascii_credentials_return_401_not_500(client: httpx.AsyncClient) -> None:
    """secrets.compare_digest 传 str 时只支持 ASCII，非 ASCII 会抛 TypeError。
    必须按普通的凭证错误返回 401，否则 500/401 的差异会泄露用户名是否存在。"""
    r = await client.post(
        "/api/auth/bootstrap", json={"username": "管理员", "password": "错误密码"}
    )
    assert r.status_code == 401
    assert r.json() == {"code": 401, "message": "用户名或密码错误"}


async def test_rate_limited_after_five_failures(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        assert (await _login(client, password="nope")).status_code == 401
    r = await _login(client)
    assert r.status_code == 429
    assert r.json()["code"] == 429


async def test_successful_logins_are_never_rate_limited(client: httpx.AsyncClient) -> None:
    """限流只记失败：成功登录不计数，否则正常使用几次就会把自己锁死。"""
    for _ in range(6):
        assert (await _login(client)).status_code == 200


async def test_bootstrap_disabled_403(client: httpx.AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    await app.state.settings_store.set("bootstrap_admin_enabled", False)
    r = await _login(client)
    assert r.status_code == 403
    assert r.json()["message"] == "引导登录已关闭"


async def test_me_requires_session(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/auth/me")
    assert r.status_code == 401
    assert r.json()["message"] == "未登录或会话已过期"


async def test_me_logout_flow(client: httpx.AsyncClient) -> None:
    login = await _login(client)
    csrf = login.cookies[CSRF_COOKIE]
    me = await client.get("/api/admin/auth/me")
    assert me.status_code == 200 and me.json()["data"]["source"] == "bootstrap"

    no_csrf = await client.post("/api/admin/auth/logout")
    assert no_csrf.status_code == 403

    out = await client.post("/api/admin/auth/logout", headers={CSRF_HEADER: csrf})
    assert out.status_code == 200 and out.json() == {"code": 0, "data": None}
    assert (await client.get("/api/admin/auth/me")).status_code == 401


async def test_tampered_cookie_401(client: httpx.AsyncClient) -> None:
    await _login(client)
    client.cookies.set(SESSION_COOKIE, "garbage")
    assert (await client.get("/api/admin/auth/me")).status_code == 401


async def test_renewal_reissues_session_cookie(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    # itsdangerous 的签名时间戳精度是整数秒；本测试全程 < 1s 真实时间，若用真实时钟，
    # 登录和续期两次签名会拿到相同的秒数、签出完全相同的 token，使下面「新旧 cookie
    # 不同」的断言产生偶发失败（已实测复现）。用单调递增的假时钟替换
    # itsdangerous 的时间源，确定性地保证续期时间戳更晚——不依赖真实时间流逝（无 sleep）。
    fake_clock = itertools.count(1)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: next(fake_clock))

    login = await _login(client)
    old_cookie = login.cookies[SESSION_COOKIE]
    # 无需续期：5 分钟内的请求不带 Set-Cookie
    fresh = await client.get("/api/admin/auth/me")
    assert fresh.status_code == 200 and SESSION_COOKIE not in fresh.cookies
    # 把 last_seen_at 拨到 10 分钟前，触发续期
    row = (await db_session.execute(select(AdminSession))).scalar_one()
    row.last_seen_at = datetime.now(UTC) - timedelta(minutes=10)
    old_expiry = row.expires_at
    await db_session.commit()
    renewed = await client.get("/api/admin/auth/me")
    assert renewed.status_code == 200
    assert SESSION_COOKIE in renewed.cookies and renewed.cookies[SESSION_COOKIE] != old_cookie
    secret = app.state.settings.session_secret
    assert unsign_session_id(secret, renewed.cookies[SESSION_COOKIE]) == row.id
    await db_session.refresh(row)
    assert row.expires_at > old_expiry
    # 新 cookie 仍可用
    assert (await client.get("/api/admin/auth/me")).status_code == 200
