"""Fill operation-level QA gaps with stateful lifecycle and permission checks."""

from sqlalchemy import select

from coreman.core.db.models import BusinessSystem, CronJob, User, UserIdentity
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import seed_bot


async def test_cron_cancel_disable_history_and_delete(client, db_session):
    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="qa-user"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    response = await client.post(
        "/api/admin/cron-jobs",
        json={
            "bot_id": str(bot.id),
            "name": "QA lifecycle",
            "cron_expression": "0 9 * * *",
            "prompt": "QA test",
            "notify_webhook": False,
        },
    )
    assert response.status_code == 201
    row = response.json()["data"]
    path = "/api/admin/cron-jobs/" + row["id"]
    history = await client.get(path + "/runs")
    assert history.status_code == 200 and history.json()["data"]["total"] == 0
    run = await client.post(path + "/run", headers={"If-Match": str(row["version"])})
    assert run.status_code == 200
    pending = run.json()["data"]
    cancelled = await client.post(
        path + "/cancel-run", headers={"If-Match": str(pending["version"])}
    )
    assert cancelled.status_code == 200
    job = await db_session.scalar(select(CronJob).execution_options(populate_existing=True))
    assert job.force_run_at is None and job.force_run_by is None
    disabled = await client.post(
        path + "/disable",
        headers={
            "If-Match": str(cancelled.json()["data"]["version"]),
        },
    )
    assert disabled.status_code == 200
    version = str(disabled.json()["data"]["version"])
    assert disabled.json()["data"]["enabled"] is False
    assert (await client.post(path + "/run", headers={"If-Match": version})).status_code == 409
    assert (await client.delete(path, headers={"If-Match": version})).status_code == 200
    assert await db_session.scalar(select(CronJob)) is None
    assert (await client.get(path + "/runs")).status_code == 404


async def test_source_edit_version_and_member_denial(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    body = {"key": "qa-source", "label": "Before"}
    created = await client.post("/api/admin/skill-sources", json=body)
    assert created.status_code == 200
    row = created.json()["data"]
    path = "/api/admin/skill-sources/" + row["id"]
    body["label"] = "After"
    assert (await client.put(path, json=body)).status_code == 428
    edited = await client.put(path, json=body, headers={"If-Match": str(row["version"])})
    assert edited.status_code == 200 and edited.json()["data"]["label"] == "After"
    assert (
        await client.put(path, json=body, headers={"If-Match": str(row["version"])})
    ).status_code == 409
    await login_as(client, db_session, role="member")
    result = await client.get("/api/admin/skill-sources")
    assert result.status_code == 200 and result.json()["data"][0]["label"] == "After"
    assert (
        await client.put(
            path,
            json=body,
            headers={
                "If-Match": str(edited.json()["data"]["version"]),
            },
        )
    ).status_code == 403


async def test_system_delete_obeys_version_and_removes_record(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    response = await client.post("/api/admin/systems", json={"key": "qa_system", "name": "QA"})
    assert response.status_code == 201
    data = response.json()["data"]
    path = "/api/admin/systems/qa_system"
    assert (await client.get("/api/admin/systems")).json()["data"]["total"] == 1
    assert (await client.delete(path)).status_code == 428
    assert (await client.delete(path, headers={"If-Match": "0"})).status_code == 409
    assert (
        await client.delete(path, headers={"If-Match": str(data["version"])})
    ).status_code == 200
    assert await db_session.scalar(select(BusinessSystem)) is None
    assert (await client.get("/api/admin/systems")).json()["data"]["total"] == 0
