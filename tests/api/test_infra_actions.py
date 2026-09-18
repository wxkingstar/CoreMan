import time

import httpx
import jwt
import pytest
from sqlalchemy import func, select

from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import Bot, BusinessSystem, OutboxItem
from tests.api.conftest import login_as


def signed(path, body, secret, app_key="actions"):
    ts = str(int(time.time()))
    return {
        "X-App-Key": app_key,
        "X-Timestamp": ts,
        "X-Signature": sign_request("POST", path, body, ts, app_key, secret),
    }


async def test_push_is_durable_idempotent_and_rejects_changed_payload(client, db_session):
    user = await login_as(client, db_session, role="platform_admin")
    credential = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Actions", "scopes": ["push"]},
        )
    ).json()["data"]
    bot = Bot(
        bot_key="push-bot",
        name="Push",
        platform="wecom",
        created_by=user.id,
        model="test",
        working_dir="/data/test",
        credentials_enc="unused",
    )
    db_session.add(bot)
    await db_session.commit()
    body = {"bot_key": bot.bot_key, "chat_id": "user1", "content": "消息", "request_id": "test1"}
    path = "/api/infra/push"
    for _ in range(2):
        r = await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "pending"
    assert (await db_session.scalar(select(func.count()).select_from(OutboxItem))) == 1
    changed = {**body, "content": "另一条消息"}
    assert (
        await client.post(path, json=changed, headers=signed(path, changed, credential["secret"]))
    ).status_code == 409
    assert (await client.post(path, json=body)).status_code == 401
    bot.enabled = False
    await db_session.commit()
    assert (
        await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
    ).status_code == 409


async def test_system_test_requires_real_current_user_and_hides_target_secrets(
    client, db_session, monkeypatch
):
    from coreman.api.routers import infra_system_test

    user = await login_as(client, db_session, role="platform_admin", login_name="real-human")
    await client.post(
        "/api/admin/systems",
        json={"key": "example", "name": "Example", "base_url": "https://example.test/"},
    )
    credential = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Actions", "scopes": ["systems"]},
        )
    ).json()["data"]
    seen = []

    def handler(req):
        if "cookie" not in req.headers:  # 先不带令牌请求一次作对照
            return httpx.Response(302, headers={"Location": "/login?next=%2F"})
        claims = jwt.decode(
            req.headers["cookie"].removeprefix("bot_token="), options={"verify_signature": False}
        )
        assert claims["sub"] == user.login_name and claims["exp"] - claims["iat"] == 60
        seen.append(req)
        return httpx.Response(
            200, text="synthetic-secret-body", headers={"Set-Cookie": "private=session"}
        )

    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    path = "/api/admin/systems/test-access"
    body = {"system_key": "example"}
    assert (
        await client.post(path, json={**body, "email_prefix": "someone-else"})
    ).status_code == 403
    for path in ("/api/admin/systems/test-access", "/api/infra/systems/test-access"):
        r = await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
        assert r.status_code == 200 and r.json()["data"]["success"], r.text
        assert "synthetic-secret" not in r.text and "private=session" not in r.text
    assert len(seen) == 2
    db_session.add(BusinessSystem(key="coreman", name="Legacy", base_url="https://example.test/"))
    await db_session.commit()
    path = "/api/admin/systems/test-access"
    assert (await client.post(path, json={"system_key": "coreman"})).status_code == 422
    assert len(seen) == 2
    client.cookies.clear()
    assert (
        await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
    ).status_code in (401, 403)
    assert len(seen) == 2


async def test_system_test_uses_email_prefix_as_subject(client, db_session, monkeypatch):
    from coreman.api.routers import infra_system_test

    # 首次同步没拿到邮箱时 login_name 是平台 userid；之后补上邮箱，令牌主体应跟着用邮箱前缀。
    user = await login_as(client, db_session, role="platform_admin", login_name="ou_1a2b3c")
    user.email = "Real.Human@example.com"
    await db_session.commit()
    await client.post(
        "/api/admin/systems",
        json={"key": "example", "name": "Example", "base_url": "https://example.test/"},
    )
    subjects = []

    def handler(req):
        if "cookie" in req.headers:
            claims = jwt.decode(
                req.headers["cookie"].removeprefix("bot_token="),
                options={"verify_signature": False},
            )
            subjects.append(claims["sub"])
        return httpx.Response(302, headers={"Location": "/login"})

    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    path = "/api/admin/systems/test-access"
    # 旧调用方传的 email_prefix 可以是登录名，也可以是邮箱前缀，但不能是别人。
    for prefix in ("real.human", "ou_1a2b3c"):
        r = await client.post(path, json={"system_key": "example", "email_prefix": prefix})
        assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert subjects == ["real.human", "real.human"]
    assert data["subject"] == "real.human" and data["user_login"] == "ou_1a2b3c"
    assert not data["success"] and "real.human" in data["message"]
    r = await client.post(path, json={"system_key": "example", "email_prefix": "someone-else"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("baseline", "with_token", "success", "hint"),
    [
        # 登录后首页照样跳转（去默认页或别的子系统）：去向与不带令牌时不同，算已识别。
        ((302, "/login"), (302, "/statistics/index"), True, "登录后跳转到 /statistics/index"),
        ((302, "/Login/index?reason=x"), (302, "http://other.example/"), True, "other.example/"),
        ((302, "/login"), (200, None), True, "目标已识别令牌"),
        # 带不带令牌都去登录页（查询参数不同也算同一处）：令牌未生效。
        ((302, "/login?reason=a"), (302, "/login?reason=b"), False, "带令牌仍跳到 /login"),
        # 不带令牌只回 401，带令牌跳到登录页：同样未生效。
        ((401, None), (302, "/signin"), False, "令牌未生效"),
        ((302, "/login"), (403, None), False, "目标未通过访问测试"),
        # 首页本来就公开：看不出令牌是否生效。
        ((200, None), (200, None), False, "无法判断令牌是否生效"),
    ],
)
async def test_system_test_compares_with_a_request_without_token(
    client, db_session, monkeypatch, baseline, with_token, success, hint
):
    from coreman.api.routers import infra_system_test

    await login_as(client, db_session, role="platform_admin", login_name="real-human")
    await client.post(
        "/api/admin/systems",
        json={"key": "example", "name": "Example", "base_url": "https://example.test/"},
    )

    def handler(req):
        status, location = with_token if "cookie" in req.headers else baseline
        return httpx.Response(status, headers={"Location": location} if location else {})

    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    r = await client.post("/api/admin/systems/test-access", json={"system_key": "example"})
    data = r.json()["data"]
    assert data["success"] is success and hint in data["message"], data
    assert data["status_code"] == with_token[0] and data["baseline_status_code"] == baseline[0]
    # 只回显主机与路径，查询参数不外露。
    assert "reason" not in r.text
