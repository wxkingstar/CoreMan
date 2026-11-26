"""业务系统代员工发起 API 任务的 HTTP 层：签名、接口组、按员工隔离的查询与取消、管理台设置。"""

from __future__ import annotations

from sqlalchemy import select

from coreman.core.db.models import ApiClient, AuditLog, User, UserIdentity, UserReached
from tests.api.conftest import login_as
from tests.api.test_escalations import Signed
from tests.integration.worker_helpers import seed_bot


async def _setup(client, session, scopes=("tasks",)):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform, bot.api_access = "feishu", True
    member = User(login_name="u-alice", display_name="爱丽丝", email="alice@example.test")
    other = User(login_name="u-bob", display_name="鲍勃", email="bob@example.test")
    session.add_all([member, other])
    await session.flush()
    for user, pid in ((member, "ou_alice"), (other, "ou_bob")):
        session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=pid))
        session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id=f"p2p-{pid}"))
    session.add(
        ApiClient(
            app_key="intake",
            name="资料录入",
            scopes=list(scopes),
            bot_ids=[bot.id],
            secret_enc=cipher.encrypt("client-secret", "api_clients.secret_enc"),
        )
    )
    await session.commit()
    client.auth = Signed("intake", "client-secret")
    return bot


async def test_signed_client_starts_queries_and_cancels_for_a_member(client, db_session):
    await _setup(client, db_session)
    body = {"user": "alice", "bot_key": "sales_bot", "prompt": "识别截图", "request_id": "f-1"}
    created = await client.post("/api/infra/tasks/run", json=body)
    assert created.status_code == 202, created.text
    task = created.json()["data"]
    assert task["user"] == "alice" and task["status"] == "queued"
    replay = await client.post("/api/infra/tasks/run", json=body)
    assert replay.status_code == 200 and replay.json()["data"]["id"] == task["id"]
    got = await client.get(f"/api/infra/tasks/run/{task['id']}", params={"user": "alice"})
    assert got.status_code == 200 and got.json()["data"]["id"] == task["id"]
    # 换成别的员工就查不到：调用方只能以发起时的那位员工查询。
    other = await client.get(f"/api/infra/tasks/run/{task['id']}", params={"user": "bob"})
    assert other.status_code == 404
    cancelled = await client.post(
        f"/api/infra/tasks/run/{task['id']}/cancel", json={"user": "alice"}
    )
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "api_task.create"))
    assert audit.diff["api_client"] == "intake"


async def test_requires_tasks_scope_and_valid_signature(client, db_session):
    await _setup(client, db_session, scopes=("push",))
    body = {"user": "alice", "bot_key": "sales_bot", "prompt": "识别截图"}
    no_scope = await client.post("/api/infra/tasks/run", json=body)
    assert no_scope.status_code == 403
    client.auth = Signed("intake", "wrong-secret")
    bad = await client.post("/api/infra/tasks/run", json=body)
    assert bad.status_code == 401


async def test_unknown_member_is_rejected(client, db_session):
    await _setup(client, db_session)
    body = {"user": "nobody", "bot_key": "sales_bot", "prompt": "识别截图"}
    denied = await client.post("/api/infra/tasks/run", json=body)
    assert denied.status_code == 403 and "邮箱前缀" in denied.json()["message"]


async def test_admin_sets_task_bots_and_update_without_them_keeps_them(client, db_session):
    bot = await _setup(client, db_session)
    client.auth = None
    await login_as(client, db_session, role="platform_admin")
    created = await client.post(
        "/api/admin/api-clients",
        json={"app_key": "crm", "name": "CRM", "scopes": ["tasks"], "bot_ids": [str(bot.id)]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["bot_ids"] == [str(bot.id)]
    row = created.json()["data"]
    kept = await client.put(
        "/api/admin/api-clients/crm",
        json={"name": "CRM 2", "scopes": ["tasks"], "enabled": True},
        headers={"If-Match": str(row["version"])},
    )
    assert kept.status_code == 200 and kept.json()["data"]["bot_ids"] == [str(bot.id)]
    bad = await client.post(
        "/api/admin/api-clients",
        json={"app_key": "x2", "name": "X", "scopes": ["tasks"], "bot_ids": [str(row["app_key"])]},
    )
    assert bad.status_code == 422


async def test_silent_request_is_signed_and_checked_against_the_registered_writes(
    client, db_session
):
    await _setup(client, db_session)
    row = await db_session.get(ApiClient, "intake")
    row.silent_writes = ["profile_admin.saveDraft"]
    await db_session.commit()
    body = {
        "user": "alice",
        "bot_key": "sales_bot",
        "prompt": "初评受众",
        "silent": True,
        "allowed_writes": ["profile_admin.saveDraft"],
    }
    created = await client.post("/api/infra/tasks/run", json=body)
    assert created.status_code == 202, created.text
    data = created.json()["data"]
    assert data["silent"] is True and data["allowed_writes"] == ["profile_admin.saveDraft"]
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "api_task.create"))
    assert audit.diff["silent"] is True
    unregistered = await client.post(
        "/api/infra/tasks/run",
        json={**body, "prompt": "改收款账户", "allowed_writes": ["profile_admin.updatePayout"]},
    )
    # 有没登记的项：不报错，整个任务回退成普通模式，视图说明原因。
    assert unregistered.status_code == 202, unregistered.text
    fallback = unregistered.json()["data"]
    assert fallback["silent"] is False
    assert fallback["silent_fallback"] == {
        "reason": "not_registered",
        "unregistered": ["profile_admin.updatePayout"],
    }
    # 签名覆盖这两个字段：按 silent=false 签名、发出去却是 true，就是签名无效。
    import time

    from coreman.core.auth.signatures import sign_request

    client.auth = None
    ts = str(int(time.time()))
    signed = {**body, "prompt": "另一件", "silent": False}
    signature = sign_request("POST", "/api/infra/tasks/run", signed, ts, "intake", "client-secret")
    tampered = await client.post(
        "/api/infra/tasks/run",
        json={**signed, "silent": True},
        headers={"X-App-Key": "intake", "X-Timestamp": ts, "X-Signature": signature},
    )
    assert tampered.status_code == 401


async def test_admin_registers_silent_writes_for_proxied_systems_only(client, db_session):
    from coreman.core.db.models import BusinessSystem

    await _setup(client, db_session)
    db_session.add_all(
        [
            BusinessSystem(
                key="profile_admin",
                name="资料录入",
                base_url="https://profiles.example.com",
                token_delivery="proxy",
            ),
            BusinessSystem(
                key="wiki", name="知识库", base_url="https://wiki.example.com", token_delivery="env"
            ),
        ]
    )
    await db_session.commit()
    client.auth = None
    await login_as(client, db_session, role="platform_admin")
    for bad in (["saveDraft"], ["nosuch.saveDraft"], ["wiki.editPage"]):
        refused = await client.post(
            "/api/admin/api-clients",
            json={"app_key": "crm", "name": "CRM", "scopes": ["tasks"], "silent_writes": bad},
        )
        assert refused.status_code == 422, (bad, refused.text)
    created = await client.post(
        "/api/admin/api-clients",
        json={
            "app_key": "crm",
            "name": "CRM",
            "scopes": ["tasks"],
            "silent_writes": ["profile_admin.saveDraft"],
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()["data"]
    assert row["silent_writes"] == ["profile_admin.saveDraft"]
    # 不带 silent_writes 的更新保留原清单；带了就整体替换，并记进审计。
    kept = await client.put(
        "/api/admin/api-clients/crm",
        json={"name": "CRM", "scopes": ["tasks"]},
        headers={"If-Match": str(row["version"])},
    )
    assert kept.json()["data"]["silent_writes"] == ["profile_admin.saveDraft"]
    cleared = await client.put(
        "/api/admin/api-clients/crm",
        json={"name": "CRM", "scopes": ["tasks"], "silent_writes": []},
        headers={"If-Match": str(kept.json()["data"]["version"])},
    )
    assert cleared.status_code == 200 and cleared.json()["data"]["silent_writes"] == []
    audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "api_client.update")
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    assert audit.diff["silent_writes"] == {"before": ["profile_admin.saveDraft"], "after": []}
    options = await client.get("/api/admin/api-clients/silent-write-options")
    assert options.status_code == 200 and options.json()["data"] == []
