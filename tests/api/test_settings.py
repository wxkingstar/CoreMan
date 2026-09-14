import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, PlatformApp
from tests.api.conftest import login_as


async def test_get_put_defaults_and_guard(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    r = await admin_client.get("/api/admin/settings")
    assert (
        r.status_code == 200
        and r.json()["data"]["session_ttl_hours"] == 72
        and r.json()["data"]["bootstrap_admin_enabled"] is True
    )
    assert (await admin_client.put("/api/admin/settings", json={})).status_code == 422
    assert (
        await admin_client.put("/api/admin/settings", json={"default_model": "nope/none"})
    ).status_code == 422
    r = await admin_client.put(
        "/api/admin/settings", json={"session_ttl_hours": 24, "default_effort_level": "high"}
    )
    assert r.status_code == 200 and r.json()["data"]["session_ttl_hours"] == 24
    # 原值重写仍是 200，但不写第二条审计（下面的 scalar_one 会替我们盯着）
    assert (
        await admin_client.put("/api/admin/settings", json={"session_ttl_hours": 24})
    ).status_code == 200
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "settings.update"))
    ).scalar_one()
    assert audit.diff == {
        "session_ttl_hours": [72, 24],
        "default_effort_level": [None, "high"],
    }
    # 没有登录应用时不能关引导登录
    r = await admin_client.put("/api/admin/settings", json={"bootstrap_admin_enabled": False})
    assert r.status_code == 422 and "登录" in r.json()["message"]
    db_session.add(
        PlatformApp(
            platform="wecom",
            name="login",
            capabilities=["login"],
            corp_id="ww",
            secret_enc="enc:v1:x",
        )
    )
    await db_session.commit()
    assert (
        await admin_client.put("/api/admin/settings", json={"bootstrap_admin_enabled": False})
    ).status_code == 200
    assert (
        await admin_client.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "pass-1234"}
        )
    ).status_code == 403


async def test_defaults_endpoint_for_member_and_settings_forbidden(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="member")
    r = await client.get("/api/admin/settings/defaults")
    assert r.status_code == 200 and set(r.json()["data"]) == {
        "default_model",
        "default_verbosity_level",
        "default_effort_level",
    }
    assert (await client.get("/api/admin/settings")).status_code == 403
    assert (
        await client.put("/api/admin/settings", json={"session_ttl_hours": 100})
    ).status_code == 403


async def test_null_only_allowed_for_effort_level(admin_client: httpx.AsyncClient) -> None:
    # null 会被存成「有值且为 None」：引导登录守卫会被绕过，默认模型会变成空
    for key in ("bootstrap_admin_enabled", "default_model", "session_ttl_hours"):
        assert (await admin_client.put("/api/admin/settings", json={key: None})).status_code == 422
    assert (
        await admin_client.put("/api/admin/settings", json={"default_effort_level": None})
    ).status_code == 200
    r = await admin_client.post(
        "/api/auth/bootstrap", json={"username": "admin", "password": "pass-1234"}
    )
    assert r.status_code == 200
