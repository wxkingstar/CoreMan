"""群承接模式的 HTTP 层：管理台配置（只能选服务账号）、发起与查询不带员工、排队满时的错误。"""

from __future__ import annotations

import hashlib
import time
import uuid

from sqlalchemy import select

from coreman.core.api_tasks import groups
from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import (
    ApiClient,
    ApiClientGroupRoute,
    ApiTaskUpload,
    AuditLog,
    BotAlertSource,
    User,
)
from tests.api.conftest import login_as
from tests.api.test_escalations import Signed
from tests.integration.worker_helpers import seed_bot


async def _setup(client, session):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform, bot.api_access = "feishu", True
    svc = User(login_name="svc-ops", display_name="运维服务账号", email="svc-ops@example.test")
    svc.source = "service"
    person = User(login_name="u-alice", display_name="爱丽丝", email="alice@example.test")
    session.add_all([svc, person])
    session.add(
        ApiClient(
            app_key="monitor",
            name="监控系统",
            scopes=["tasks"],
            bot_ids=[bot.id],
            secret_enc=cipher.encrypt("client-secret", "api_clients.secret_enc"),
        )
    )
    await session.flush()
    session.add(
        BotAlertSource(
            bot_id=bot.id, chat_id="oc_alerts", sender_union_id="on_x", service_user_id=svc.id
        )
    )
    await session.commit()
    return bot, svc, person


async def test_admin_configures_group_routes_with_service_accounts_only(client, db_session):
    bot, svc, person = await _setup(client, db_session)
    await login_as(client, db_session, role="platform_admin")
    options = await client.get(
        "/api/admin/api-clients/group-route-options", params={"bot_id": str(bot.id)}
    )
    assert options.status_code == 200, options.text
    data = options.json()["data"]
    assert data["alert_sources"] == [{"chat_id": "oc_alerts", "service_user_id": str(svc.id)}]
    assert [u["id"] for u in data["service_users"]] == [str(svc.id)]

    url = "/api/admin/api-clients/monitor/group-routes"
    person_route = await client.post(
        url, json={"bot_id": str(bot.id), "chat_id": "oc_alerts", "service_user_id": str(person.id)}
    )
    assert person_route.status_code == 422 and "服务账号" in person_route.json()["message"]
    created = await client.post(
        url, json={"bot_id": str(bot.id), "chat_id": "oc_alerts", "service_user_id": str(svc.id)}
    )
    assert created.status_code == 201, created.text
    route = created.json()["data"]
    assert route["chat_id"] == "oc_alerts" and route["service_user"] == "运维服务账号"
    again = await client.post(
        url, json={"bot_id": str(bot.id), "chat_id": "oc_alerts", "service_user_id": str(svc.id)}
    )
    assert again.status_code == 409
    listed = await client.get(url)
    assert [r["id"] for r in listed.json()["data"]] == [route["id"]]
    clients = await client.get("/api/admin/api-clients")
    assert clients.json()["data"]["items"][0]["group_routes"] == 1
    removed = await client.delete(f"{url}/{route['id']}")
    assert removed.status_code == 200
    assert await db_session.scalar(select(ApiClientGroupRoute)) is None
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {"api_client.group_route.create", "api_client.group_route.delete"} <= actions


async def test_bot_must_be_drivable_by_the_client(client, db_session):
    bot, svc, _ = await _setup(client, db_session)
    monitor = await db_session.get(ApiClient, "monitor")
    monitor.bot_ids = []
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    refused = await client.post(
        "/api/admin/api-clients/monitor/group-routes",
        json={"bot_id": str(bot.id), "chat_id": "oc_alerts", "service_user_id": str(svc.id)},
    )
    assert refused.status_code == 422


async def test_only_platform_admins_manage_group_routes(client, db_session):
    await _setup(client, db_session)
    await login_as(client, db_session, role="member")
    denied = await client.get("/api/admin/api-clients/monitor/group-routes")
    assert denied.status_code == 403


async def test_signed_client_runs_queries_and_cancels_without_a_member(client, db_session):
    bot, svc, _ = await _setup(client, db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_alerts", service_user_id=svc.id
        )
    )
    await db_session.commit()
    client.auth = Signed("monitor", "client-secret")
    with_user = await client.post(
        "/api/infra/tasks/run", json={"user": "alice", "bot_key": "sales_bot", "prompt": "查一下"}
    )
    assert with_user.status_code == 422
    body = {"bot_key": "sales_bot", "prompt": "查一下", "request_id": "alert-9"}
    created = await client.post("/api/infra/tasks/run", json=body)
    assert created.status_code == 202, created.text
    task = created.json()["data"]
    assert task["conversation"] == {"target": "chat", "chat_id": "oc_alerts"}
    replay = await client.post("/api/infra/tasks/run", json=body)
    assert replay.status_code == 200 and replay.json()["data"]["id"] == task["id"]
    got = await client.get(f"/api/infra/tasks/run/{task['id']}")
    assert got.status_code == 200 and got.json()["data"]["id"] == task["id"]
    as_user = await client.get(f"/api/infra/tasks/run/{task['id']}", params={"user": "alice"})
    assert as_user.status_code == 422
    cancelled = await client.post(f"/api/infra/tasks/run/{task['id']}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "api_task.create"))
    assert audit.actor_login == "svc-ops" and audit.diff["chat_id"] == "oc_alerts"


async def test_full_queue_answers_429_queue_full(client, db_session, monkeypatch):
    bot, svc, _ = await _setup(client, db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_alerts", service_user_id=svc.id
        )
    )
    await db_session.commit()
    monkeypatch.setattr(groups, "QUEUE_MAX", 1)
    client.auth = Signed("monitor", "client-secret")
    first = await client.post("/api/infra/tasks/run", json={"bot_key": "sales_bot", "prompt": "1"})
    assert first.status_code == 202
    full = await client.post("/api/infra/tasks/run", json={"bot_key": "sales_bot", "prompt": "2"})
    assert full.status_code == 429
    assert full.json()["errors"] == [{"type": "queue_full"}]


async def test_group_mode_uploads_belong_to_the_service_account(client, db_session):

    bot, svc, _ = await _setup(client, db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_alerts", service_user_id=svc.id
        )
    )
    await db_session.commit()
    path = "/api/infra/tasks/uploads"
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    async def upload(fields):  # type: ignore[no-untyped-def]
        ts = str(int(time.time()))
        headers = {
            "X-App-Key": "monitor",
            "X-Timestamp": ts,
            "X-Signature": sign_request("POST", path, fields, ts, "monitor", "client-secret"),
        }
        return await client.post(
            path, data=fields, files={"file": ("a.png", data, "image/png")}, headers=headers
        )

    sha = hashlib.sha256(data).hexdigest()
    with_user = await upload({"user": "alice", "bot_key": "sales_bot", "sha256": sha})
    assert with_user.status_code == 422
    done = await upload({"bot_key": "sales_bot", "sha256": sha})
    assert done.status_code == 201, done.text
    upload_id = done.json()["data"]["upload_id"]
    row = await db_session.get(ApiTaskUpload, uuid.UUID(upload_id))
    assert row.user_id == svc.id and row.client_key == "monitor"
    client.auth = Signed("monitor", "client-secret")
    created = await client.post(
        "/api/infra/tasks/run",
        json={"bot_key": "sales_bot", "prompt": "看看这张图", "images": [upload_id]},
    )
    assert created.status_code == 202, created.text


async def _routed(client, db_session):  # type: ignore[no-untyped-def]
    bot, svc, _ = await _setup(client, db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_alerts", service_user_id=svc.id
        )
    )
    await db_session.commit()
    client.auth = Signed("monitor", "client-secret")
    return bot


async def _start(client, prompt):  # type: ignore[no-untyped-def]
    created = await client.post(
        "/api/infra/tasks/run", json={"bot_key": "sales_bot", "prompt": prompt}
    )
    assert created.status_code == 202, created.text
    return created.json()["data"]["id"]


async def test_client_lists_its_open_tasks_and_how_busy_the_group_is(client, db_session):
    from coreman.core.db.models import ApiTask, Task

    await _routed(client, db_session)
    first = await _start(client, "告警 1")
    second = await _start(client, "告警 2")
    # 第一条已经开跑。
    row = await db_session.get(ApiTask, uuid.UUID(first))
    task = await db_session.get(Task, row.task_id)
    task.status = "running"
    await db_session.commit()
    listed = await client.get("/api/infra/tasks/run")
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert [(t["id"], t["status"]) for t in data["items"]] == [
        (first, "running"),
        (second, "queued"),
    ]
    assert data["groups"] == [
        {
            "bot_key": "sales_bot",
            "chat_id": "oc_alerts",
            "busy": True,
            "running_mine": True,
            "queued": 1,
            "queue_max": 5,
        }
    ]
    queued = await client.get("/api/infra/tasks/run", params={"state": "queued"})
    assert [t["id"] for t in queued.json()["data"]["items"]] == [second]


async def test_client_cancels_queued_and_running_tasks_in_bulk(client, db_session):
    from coreman.core.db.models import ApiTask, OutboxItem, Task

    await _routed(client, db_session)
    first = await _start(client, "告警 1")
    second = await _start(client, "告警 2")
    third = await _start(client, "告警 3")
    row = await db_session.get(ApiTask, uuid.UUID(first))
    running_id = row.task_id
    task = await db_session.get(Task, running_id)
    task.status = "running"
    await db_session.commit()
    # 先只撤排队的：执行中的那条不动。
    queued = await client.post("/api/infra/tasks/run/cancel", json={"state": "queued"})
    assert queued.status_code == 200, queued.text
    assert sorted(queued.json()["data"]["cancelled"]) == sorted([second, third])
    # 再按 id 停掉执行中的那条；已经取消的再给一次不算。
    running = await client.post("/api/infra/tasks/run/cancel", json={"ids": [first, second]})
    assert running.json()["data"]["cancelled"] == [first]
    db_session.expire_all()
    stopped = await db_session.get(Task, running_id)
    assert stopped.cancel_requested_at is not None and stopped.cancel_reason == "api_cancel"
    notices = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.dedupe_key.like("api-task:%:cancelled"))
        )
    )
    assert len(notices) == 3 and all(n.target == {"chat_id": "oc_alerts"} for n in notices)
    assert "「监控系统」取消了这条任务" in notices[0].payload["markdown"]
    left = await client.get("/api/infra/tasks/run")
    assert left.json()["data"]["items"] == []
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "api_task.cancel"))
    assert audit is not None and audit.actor_login == "monitor"


async def test_a_client_cannot_see_or_cancel_another_clients_tasks(client, db_session):
    from coreman.core.db.models import Task

    bot = await _routed(client, db_session)
    mine = await _start(client, "告警 1")
    db_session.add(
        ApiClient(
            app_key="other",
            name="别的系统",
            scopes=["tasks"],
            bot_ids=[bot.id],
            secret_enc=(await db_session.get(ApiClient, "monitor")).secret_enc,
        )
    )
    await db_session.commit()
    client.auth = Signed("other", "client-secret")
    assert (await client.get("/api/infra/tasks/run")).json()["data"] == {
        "items": [],
        "groups": [],
    }
    refused = await client.post("/api/infra/tasks/run/cancel", json={"ids": [mine]})
    assert refused.json()["data"]["cancelled"] == []
    db_session.expire_all()
    still = await db_session.scalar(select(Task).where(Task.cancel_requested_at.is_not(None)))
    assert still is None


async def test_group_mode_rejects_silent_but_keeps_allowed_writes(client, db_session):
    bot, svc, _ = await _setup(client, db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_alerts", service_user_id=svc.id
        )
    )
    monitor = await db_session.get(ApiClient, "monitor")
    monitor.silent_writes = ["ops.restart"]
    await db_session.commit()
    client.auth = Signed("monitor", "client-secret")
    body = {"bot_key": "sales_bot", "prompt": "查一下", "allowed_writes": ["ops.restart"]}
    silent = await client.post("/api/infra/tasks/run", json={**body, "silent": True})
    assert silent.status_code == 422, silent.text
    assert silent.json()["errors"] == [{"type": "silent_group"}]
    loud = await client.post("/api/infra/tasks/run", json=body)
    assert loud.status_code == 202, loud.text
    assert loud.json()["data"]["allowed_writes"] == ["ops.restart"]
    assert loud.json()["data"]["silent"] is False
