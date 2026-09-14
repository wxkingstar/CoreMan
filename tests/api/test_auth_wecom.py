import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.security import CSRF_COOKIE, SESSION_COOKIE
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AdminSession,
    AuditLog,
    AuthNonce,
    PlatformApp,
    User,
    UserIdentity,
)
from coreman.core.platforms import wecom
from tests.api.conftest import MASTER_KEY


async def _seed(db_session: AsyncSession, *, status: str = "active") -> None:
    db_session.add(
        PlatformApp(
            platform="wecom",
            name="登录",
            capabilities=["login"],
            corp_id="ww1",
            app_id="1000002",
            secret_enc=Cipher(base64.b64decode(MASTER_KEY)).encrypt(
                "s", "platform_apps.secret_enc"
            ),
        )
    )
    u = User(login_name="zhangsan", display_name="张三", status=status)
    db_session.add(u)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zhangsan"))
    await db_session.commit()


def _fake_userinfo(userid: str | None):  # type: ignore[no-untyped-def]
    async def f(self: wecom.WeComClient, code: str) -> dict[str, object]:
        assert code == "code-1"
        return {"errcode": 0, "userid": userid} if userid else {"errcode": 0, "openid": "o"}

    return f


async def test_providers(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    assert (await client.get("/api/auth/providers")).json()["data"] == {
        "wecom": False,
        "feishu": False,
    }
    await _seed(db_session)
    assert (await client.get("/api/auth/providers")).json()["data"] == {
        "wecom": True,
        "feishu": False,
    }


async def test_start_builds_qr_and_oauth_urls(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed(db_session)
    r = await client.get("/api/auth/wecom/start", params={"mode": "qr", "redirect": "/bots?x=1"})
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    q = parse_qs(u.query)
    assert (
        u.netloc == "login.work.weixin.qq.com"
        and q["appid"] == ["ww1"]
        and q["agentid"] == ["1000002"]
    )
    assert q["redirect_uri"] == ["http://testserver/api/auth/wecom/callback"]
    nonce = (await db_session.execute(select(AuthNonce))).scalar_one()
    assert nonce.value == q["state"][0]
    assert nonce.payload["redirect"] == "/bots?x=1" and nonce.payload["mode"] == "qr"
    # 绑定值只存 sha256，cookie 里才是明文
    assert len(nonce.payload["bind"]) == 64
    set_cookie = r.headers["set-cookie"]
    assert "coreman_oauth=" in set_cookie and "HttpOnly" in set_cookie
    r2 = await client.get(
        "/api/auth/wecom/start", params={"mode": "oauth", "redirect": "//evil.example.com"}
    )
    assert urlparse(r2.headers["location"]).netloc == "open.weixin.qq.com"
    nonces = (
        (await db_session.execute(select(AuthNonce).order_by(AuthNonce.created_at))).scalars().all()
    )
    assert nonces[-1].payload["redirect"] == "/"


async def test_start_without_login_app(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/auth/wecom/start", params={"mode": "qr"})
    assert r.status_code == 302 and r.headers["location"] == "/login?error=wecom_not_configured"


async def test_callback_success_and_replay(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    await _seed(db_session)
    monkeypatch.setattr(wecom.WeComClient, "user_info_by_code", _fake_userinfo("zhangsan"))
    start = await client.get(
        "/api/auth/wecom/start", params={"mode": "oauth", "redirect": "/users"}
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    r = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": state})
    assert r.status_code == 302 and r.headers["location"] == "/users"
    assert SESSION_COOKIE in r.cookies and CSRF_COOKIE in r.cookies
    me = await client.get("/api/admin/auth/me")
    assert me.status_code == 200 and me.json()["data"]["login_name"] == "zhangsan"
    sess = (await db_session.execute(select(AdminSession))).scalar_one()
    assert sess.auth_method == "wecom_oauth"
    assert (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "auth.wecom_login"))
    ).scalar_one().actor_login == "zhangsan"
    # state 单次使用；code 防重放
    again = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": state})
    assert again.headers["location"] == "/login?error=invalid_state"
    start2 = await client.get("/api/auth/wecom/start", params={"mode": "qr"})
    state2 = parse_qs(urlparse(start2.headers["location"]).query)["state"][0]
    replay = await client.get(
        "/api/auth/wecom/callback", params={"code": "code-1", "state": state2}
    )
    assert replay.headers["location"] == "/login?error=code_replayed"


async def test_callback_requires_binding_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """state 必须绑定发起登录的那个浏览器（登录 CSRF）：攻击者把自己流程的 code+state
    交给受害者时，受害者浏览器没有 start 下发的 coreman_oauth，回调必须拒绝。"""
    await _seed(db_session)
    monkeypatch.setattr(wecom.WeComClient, "user_info_by_code", _fake_userinfo("zhangsan"))
    start = await client.get("/api/auth/wecom/start", params={"mode": "qr"})
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.cookies.clear()
    r = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": state})
    assert r.status_code == 302 and r.headers["location"] == "/login?error=invalid_state"
    assert SESSION_COOKIE not in r.cookies
    # nonce 仍然被消费掉：绑定校验在删除 nonce 之后，state 依旧单次使用
    assert (
        await db_session.execute(select(AuthNonce).where(AuthNonce.kind == "wecom_state"))
    ).first() is None
    # 伪造的绑定值同样不行
    start2 = await client.get("/api/auth/wecom/start", params={"mode": "qr"})
    state2 = parse_qs(urlparse(start2.headers["location"]).query)["state"][0]
    client.cookies.clear()
    client.cookies.set("coreman_oauth", "attacker-value")
    r2 = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": state2})
    assert r2.headers["location"] == "/login?error=invalid_state"
    assert SESSION_COOKIE not in r2.cookies


async def test_callback_unknown_and_disabled_user(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    await _seed(db_session, status="disabled")
    monkeypatch.setattr(wecom.WeComClient, "user_info_by_code", _fake_userinfo("nobody"))
    st = parse_qs(
        urlparse(
            (await client.get("/api/auth/wecom/start", params={"mode": "qr"})).headers["location"]
        ).query
    )["state"][0]
    r = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": st})
    assert (
        r.headers["location"] == "/login?error=user_not_found" and SESSION_COOKIE not in r.cookies
    )
    monkeypatch.setattr(wecom.WeComClient, "user_info_by_code", _fake_userinfo("zhangsan"))
    st = parse_qs(
        urlparse(
            (await client.get("/api/auth/wecom/start", params={"mode": "qr"})).headers["location"]
        ).query
    )["state"][0]
    r = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": st})
    assert r.headers["location"] == "/login?error=user_disabled"
    monkeypatch.setattr(wecom.WeComClient, "user_info_by_code", _fake_userinfo(None))
    st = parse_qs(
        urlparse(
            (await client.get("/api/auth/wecom/start", params={"mode": "qr"})).headers["location"]
        ).query
    )["state"][0]
    r = await client.get("/api/auth/wecom/callback", params={"code": "code-1", "state": st})
    assert r.headers["location"] == "/login?error=not_member"


async def test_expired_state(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    db_session.add(
        AuthNonce(
            kind="wecom_state",
            value="old",
            payload={"redirect": "/", "mode": "qr"},
            created_at=datetime.now(UTC) - timedelta(minutes=11),
        )
    )
    await db_session.commit()
    r = await client.get("/api/auth/wecom/callback", params={"code": "c", "state": "old"})
    assert r.headers["location"] == "/login?error=invalid_state"


def test_safe_redirect_rejects_external_targets() -> None:
    from coreman.api.routers.auth_wecom import _safe_redirect

    assert _safe_redirect("/users?x=1") == "/users?x=1"
    for bad in (
        None,
        "",
        "//evil.com",
        "/\\evil.com",
        "https://evil.com",
        "users",
        "\\\\evil.com",
    ):
        assert _safe_redirect(bad) == "/"
