import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher, is_encrypted
from coreman.core.db.models import AuditLog, ContactSyncRun, PlatformApp
from coreman.core.platforms import wecom
from tests.api.conftest import MASTER_KEY

BODY = {
    "platform": "wecom",
    "name": "通讯录同步",
    "capabilities": ["contact_sync", "login"],
    "corp_id": "ww123",
    "app_id": "1000002",
    "secret": "supersecret-value",
    "extra": {},
    "enabled": True,
}


async def test_crud_masks_and_encrypts(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    r = await admin_client.post("/api/admin/platform-apps", json=BODY)
    assert r.status_code == 201, r.text
    out = r.json()["data"]
    assert out["secret"] == "su••••ue" and out["version"] == 1 and out["callback_token"] is None
    row = (await db_session.execute(select(PlatformApp))).scalar_one()
    assert is_encrypted(row.secret_enc) and "supersecret" not in row.secret_enc
    import base64

    assert (
        Cipher(base64.b64decode(MASTER_KEY)).decrypt(row.secret_enc, "platform_apps.secret_enc")
        == "supersecret-value"
    )

    lst = await admin_client.get("/api/admin/platform-apps")
    assert lst.json()["data"]["total"] == 1

    # 提交脱敏值 = 不修改密钥；名称变化进审计 diff，密钥不落明文
    upd = await admin_client.put(
        f"/api/admin/platform-apps/{out['id']}",
        json={**BODY, "name": "改名", "secret": "su••••ue"},
        headers={"If-Match": '"1"'},
    )
    assert (
        upd.status_code == 200
        and upd.json()["data"]["version"] == 2
        and upd.headers["ETag"] == '"2"'
    )
    await db_session.refresh(row)
    assert (
        Cipher(base64.b64decode(MASTER_KEY)).decrypt(row.secret_enc, "platform_apps.secret_enc")
        == "supersecret-value"
    )
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "platform_app.update"))
    ).scalar_one()
    assert audit.diff == {"name": ["通讯录同步", "改名"]}

    stale = await admin_client.put(
        f"/api/admin/platform-apps/{out['id']}", json=BODY, headers={"If-Match": '"1"'}
    )
    assert stale.status_code == 409

    assert (await admin_client.delete(f"/api/admin/platform-apps/{out['id']}")).status_code == 200
    assert (await admin_client.get(f"/api/admin/platform-apps/{out['id']}")).status_code == 404
    # 删除要留下被删应用的非密钥字段快照，且不含任何密钥
    deleted = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "platform_app.delete"))
    ).scalar_one()
    assert deleted.diff is not None and deleted.diff["name"] == ["改名", None]
    assert deleted.diff["corp_id"] == ["ww123", None]
    assert not set(deleted.diff) & {"secret", "callback_token", "callback_aes_key"}


async def test_validation(admin_client: httpx.AsyncClient) -> None:
    bad = await admin_client.post(
        "/api/admin/platform-apps", json={**BODY, "capabilities": ["bogus"]}
    )
    assert bad.status_code == 422
    no_corp = await admin_client.post("/api/admin/platform-apps", json={**BODY, "corp_id": None})
    assert no_corp.status_code == 422
    masked_secret = await admin_client.post(
        "/api/admin/platform-apps", json={**BODY, "secret": "su••••ue"}
    )
    assert masked_secret.status_code == 422, masked_secret.text
    # 创建时没有旧密文可回退，任何一个密钥字段是脱敏值都必须拒绝（不能静默置空）
    masked_token = await admin_client.post(
        "/api/admin/platform-apps", json={**BODY, "callback_token": "ab••••yz"}
    )
    assert masked_token.status_code == 422, masked_token.text


async def test_member_forbidden(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/admin/platform-apps")).status_code == 401


async def test_test_connection(admin_client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    created = (await admin_client.post("/api/admin/platform-apps", json=BODY)).json()["data"]

    async def fake_token(self: wecom.WeComClient, *, force: bool = False) -> str:
        assert self._secret == "supersecret-value"
        return "T"

    monkeypatch.setattr(wecom.WeComClient, "get_token", fake_token)
    r = await admin_client.post(f"/api/admin/platform-apps/{created['id']}/test")
    assert r.status_code == 200 and r.json()["data"]["ok"] is True

    async def bad_token(self: wecom.WeComClient, *, force: bool = False) -> str:
        raise wecom.WeComError(40001, "invalid credential")

    monkeypatch.setattr(wecom.WeComClient, "get_token", bad_token)
    r = await admin_client.post(f"/api/admin/platform-apps/{created['id']}/test")
    assert r.json()["data"] == {"ok": False, "message": "企微接口错误 40001: invalid credential"}


async def test_sync_endpoints(
    admin_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from coreman.core.contacts import runner
    from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser

    async def fake_fetch(client: object) -> Directory:
        # 空目录会被 sync 直接判为抓取异常并中止（F7），这里返回一份正常的单人目录
        return Directory(
            "wecom",
            [DirectoryDept("1", None, "公司", 1)],
            [DirectoryUser("a", "A", ["1"], "1", None, None, None, None, True, {})],
        )

    monkeypatch.setattr(runner, "fetch_wecom_directory", fake_fetch)
    created = (await admin_client.post("/api/admin/platform-apps", json=BODY)).json()["data"]
    r = await admin_client.post(f"/api/admin/platform-apps/{created['id']}/sync")
    assert r.status_code == 202, r.text
    run_id = r.json()["data"]["run_id"]
    import asyncio

    for _ in range(50):
        got = (await admin_client.get(f"/api/admin/sync-runs/{run_id}")).json()["data"]
        if got["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert got["status"] == "success" and got["stats"]["users_total"] == 1
    runs_resp = await admin_client.get(f"/api/admin/platform-apps/{created['id']}/sync-runs")
    runs = runs_resp.json()["data"]
    assert [x["id"] for x in runs] == [run_id]
    neg_limit = await admin_client.get(
        f"/api/admin/platform-apps/{created['id']}/sync-runs?limit=-1"
    )
    assert neg_limit.status_code == 422
    over_limit = await admin_client.get(
        f"/api/admin/platform-apps/{created['id']}/sync-runs?limit=1000"
    )
    assert over_limit.status_code == 422
    # 触发同步要留审计（target 是应用，diff 带 run_id）
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "platform_app.sync"))
    ).scalar_one()
    assert audit.target_id == created["id"] and audit.diff == {"run_id": [None, run_id]}
    feishu_body = {**BODY, "platform": "feishu", "corp_id": None, "app_id": "cli_x"}
    feishu = (await admin_client.post("/api/admin/platform-apps", json=feishu_body)).json()["data"]

    async def fake_feishu_fetch(client: object) -> Directory:
        directory = await fake_fetch(client)
        directory.platform = "feishu"
        return directory

    monkeypatch.setattr(runner, "fetch_feishu_directory", fake_feishu_fetch)
    feishu_sync = await admin_client.post(f"/api/admin/platform-apps/{feishu['id']}/sync")
    assert feishu_sync.status_code == 202
    for _ in range(50):
        got = (
            await admin_client.get(f"/api/admin/sync-runs/{feishu_sync.json()['data']['run_id']}")
        ).json()["data"]
        if got["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert got["status"] == "success"


async def test_sync_audit_and_run_are_atomic(
    app: FastAPI, admin_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """审计与 run 行必须同事务：审计写失败时 run 行也不能留下。"""
    from coreman.api.routers import platform_apps as mod

    created = (await admin_client.post("/api/admin/platform-apps", json=BODY)).json()["data"]

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(mod, "record_audit", boom)
    # ServerErrorMiddleware 发完 500 信封后仍会重抛原异常（好让服务端记录），
    # ASGITransport 默认把它透传给调用方；这里只关心响应与落库，所以关掉透传。
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        cookies=admin_client.cookies,
        headers=admin_client.headers,
    ) as c:
        r = await c.post(f"/api/admin/platform-apps/{created['id']}/sync")
    assert r.status_code == 500
    assert (await db_session.execute(select(ContactSyncRun))).scalars().all() == []
