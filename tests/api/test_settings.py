import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, ModelCatalog, PlatformApp, Setting
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
    # null 会被存成「有值且为 None」：引导登录守卫会被绕过
    for key in ("bootstrap_admin_enabled", "session_ttl_hours"):
        assert (await admin_client.put("/api/admin/settings", json={key: None})).status_code == 422
    assert (
        await admin_client.put("/api/admin/settings", json={"default_effort_level": None})
    ).status_code == 200
    r = await admin_client.post(
        "/api/auth/bootstrap", json={"username": "admin", "password": "pass-1234"}
    )
    assert r.status_code == 200


async def _catalog(db_session: AsyncSession, *rows: tuple[str, str, bool, int]) -> None:
    """把迁移种子全部退役，再放入用例自己的目录行（provider, model, is_default, sort_order）。"""
    await db_session.execute(update(ModelCatalog).values(retired=True, is_default=False))
    db_session.add_all(
        ModelCatalog(provider=p, model=m, is_default=d, sort_order=o) for p, m, d, o in rows
    )
    await db_session.commit()


async def test_default_model_is_derived_from_catalog(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """默认模型只在模型目录维护：claude 的默认优先，其次 codex；settings 表遗留行被忽略。"""
    await _catalog(
        db_session,
        ("claude", "acme/claude-a", False, 20),
        ("claude", "acme/claude-b", True, 10),
        ("codex", "codex/x", True, 10),
    )
    # 旧版本写入的 settings 行不再生效
    db_session.add(Setting(key="default_model", value="legacy/model"))
    await db_session.commit()

    async def current() -> object:
        r = await admin_client.get("/api/admin/settings")
        assert r.status_code == 200, r.text
        return r.json()["data"]["default_model"]

    assert await current() == "acme/claude-b"
    # claude 的默认被退役：回落到 claude 未退役行里排序最前的
    await db_session.execute(
        update(ModelCatalog)
        .where(ModelCatalog.model == "acme/claude-b")
        .values(retired=True, is_default=False)
    )
    await db_session.commit()
    assert await current() == "acme/claude-a"
    # claude 全部退役：取 codex 的默认
    await db_session.execute(
        update(ModelCatalog).where(ModelCatalog.provider == "claude").values(retired=True)
    )
    await db_session.commit()
    assert await current() == "codex/x"
    await db_session.execute(update(ModelCatalog).values(retired=True, is_default=False))
    await db_session.commit()
    assert await current() is None


async def test_default_model_is_not_writable(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _catalog(db_session, ("claude", "acme/claude-a", True, 10))
    for body in (
        {"default_model": "acme/claude-a"},
        {"default_model": "nope/none"},
        {"default_model": None},
        {"default_model": "acme/claude-a", "session_ttl_hours": 24},
    ):
        r = await admin_client.put("/api/admin/settings", json=body)
        assert r.status_code == 422, body
    # 连带的合法键也没有写进去，也没有 settings 行与审计
    data = (await admin_client.get("/api/admin/settings")).json()["data"]
    assert data["session_ttl_hours"] == 72 and data["default_model"] == "acme/claude-a"
    assert (
        await db_session.execute(select(Setting).where(Setting.key == "default_model"))
    ).first() is None
    assert (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "settings.update"))
    ).first() is None
    # PUT 的回显同样带派生值
    r = await admin_client.put("/api/admin/settings", json={"session_ttl_hours": 24})
    assert r.status_code == 200 and r.json()["data"]["default_model"] == "acme/claude-a"


async def test_defaults_endpoint_derives_default_model(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _catalog(db_session, ("codex", "codex/only", True, 10))
    await login_as(client, db_session, role="member")
    r = await client.get("/api/admin/settings/defaults")
    assert r.status_code == 200 and r.json()["data"] == {
        "default_model": "codex/only",
        "default_verbosity_level": 1,
        "default_effort_level": None,
    }
