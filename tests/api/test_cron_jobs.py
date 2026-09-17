import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import BotMember, CronJob, Setting, UserIdentity, UserReached
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import seed_bot


async def test_cron_creator_edit_manual_actor_and_version(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import User

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="creator"))
    db_session.add(UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    body = {
        "bot_id": str(bot.id),
        "name": "Daily",
        "cron_expression": "0 9 * * *",
        "prompt": "report",
    }
    path = "/api/admin/cron-jobs"
    result = await client.post(path, json=body)
    assert result.status_code == 201, result.text
    data = result.json()["data"]
    job_id = data["id"]
    assert data["created_by"] == str(creator.id) and data["can_edit"]
    assert (await client.put(f"{path}/{job_id}", json=body)).status_code == 428
    assert (
        await client.put(f"{path}/{job_id}", json=body, headers={"If-Match": '"0"'})
    ).status_code == 409
    intruder = await login_as(client, db_session, role="platform_admin")
    assert (
        await client.post(f"{path}/{job_id}/run", headers={"If-Match": '"1"'})
    ).status_code == 403
    assert (await client.get(path)).json()["data"]["items"] == []
    db_session.add_all(
        [
            BotMember(bot_id=bot.id, user_id=intruder.id),
            UserIdentity(user_id=intruder.id, platform="wecom", platform_user_id="clicker"),
        ]
    )
    await db_session.commit()
    assert (
        await client.put(f"{path}/{job_id}", json=body, headers={"If-Match": '"1"'})
    ).status_code == 403
    result = await client.post(f"{path}/{job_id}/run", headers={"If-Match": '"1"'})
    assert result.status_code == 200, result.text
    row = await db_session.scalar(select(CronJob))
    assert row.force_run_by == intruder.id and row.created_by == creator.id
    await login_existing(client, db_session, creator)
    assert (
        await client.put(f"{path}/{job_id}", json=body, headers={"If-Match": f'"{row.version}"'})
    ).status_code == 409


async def test_precheck_endpoint_rejects_execution(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session)
    result = await client.post(
        "/api/admin/cron-jobs/precheck/test",
        json={"script": 'def should_trigger(ctx):\n return open("/etc/passwd")'},
    )
    assert result.status_code == 200
    assert result.json()["data"]["status"] == "failed_precheck"


async def test_notification_settings_secret_and_destination_binding(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    path = "/api/admin/notification-settings"
    body = {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 465,
        "security": "tls",
        "username": "bot@example.com",
        "sender": "bot@example.com",
        "password": "synthetic-only-secret",
    }
    result = await client.put(path, json=body, headers={"If-Match": '"0"'})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["has_password"]
    assert "synthetic-only-secret" not in result.text and "password_enc" not in result.text
    row = await db_session.get(Setting, "notification_smtp")
    assert row.value["password_enc"] != body["password"]
    body.pop("password")
    body["host"] = "other.example.com"
    body["enabled"] = False
    result = await client.put(path, json=body, headers={"If-Match": '"1"'})
    assert result.status_code == 422  # 停用后改目标也不能搬运旧密码。
    await login_as(client, db_session)
    assert (await client.get(path)).status_code == 403


async def test_job_webhook_is_private_independent_and_testable(
    client: httpx.AsyncClient, db_session: AsyncSession, app
) -> None:
    from coreman.core.db.models import OutboxItem, User

    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    creator = await db_session.get(User, bot.created_by)
    db_session.add(
        UserIdentity(user_id=creator.id, platform="feishu", platform_user_id="fs-creator")
    )
    db_session.add(UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    path = "/api/admin/cron-jobs"
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-job-secret"
    body = {
        "bot_id": str(bot.id),
        "name": "独立通知",
        "cron_expression": "0 9 * * *",
        "prompt": "report",
        "notify_webhook": True,
        "notify_webhook_url": hook,
        "target_chats": ["feishu-group"],
    }
    result = await client.post(path, json=body)
    assert result.status_code == 201, result.text
    data = result.json()["data"]
    assert data["has_webhook_url"] and "synthetic-job-secret" not in result.text
    assert "notify_webhook_url_enc" not in result.text
    row = await db_session.get(CronJob, __import__("uuid").UUID(data["id"]))
    assert row.notify_webhook_url_enc != hook
    assert app.state.cipher.decrypt(row.notify_webhook_url_enc, "notifications.webhook_url") == hook
    body.pop("notify_webhook_url")
    result = await client.put(
        f"{path}/{row.id}", json=body, headers={"If-Match": f'"{data["version"]}"'}
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["has_webhook_url"]
    # 新任务不能继承其他任务或员工的通知地址。
    second = await client.post(path, json={**body, "notify_webhook": False})
    assert second.status_code == 201 and not second.json()["data"]["has_webhook_url"]
    result = await client.post(
        f"{path}/{row.id}/test-notification", headers={"If-Match": f'"{data["version"]}"'}
    )
    assert result.status_code == 200, result.text
    ids = result.json()["data"]["outbox_ids"]
    items = (await db_session.scalars(select(OutboxItem).where(OutboxItem.id.in_(ids)))).all()
    assert {i.platform for i in items} == {"feishu", "wecom"}
    assert all("synthetic-job-secret" not in str(i.target) for i in items)
    statuses = await client.get(f"{path}/{row.id}/notification-tests")
    assert {i["channel"] for i in statuses.json()["data"]} == {"feishu", "webhook"}
    assert "synthetic-job-secret" not in statuses.text and "url_enc" not in statuses.text
    # 清除后关闭通知，不再保留凭证。
    result = await client.put(
        f"{path}/{row.id}",
        json={**body, "notify_webhook": False, "notify_webhook_url": ""},
        headers={"If-Match": f'"{data["version"]}"'},
    )
    assert result.status_code == 200 and not result.json()["data"]["has_webhook_url"]
    # 未授权用户既不能测试，也不能读投递记录。
    await login_as(client, db_session)
    assert (await client.get(f"{path}/{row.id}/notification-tests")).status_code == 403
    assert (await client.post(f"{path}/{row.id}/test-notification")).status_code == 403


async def test_notification_validation_and_platform_recipients(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import User

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="owner"))
    db_session.add(UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    body = {
        "bot_id": str(bot.id),
        "name": "通知",
        "cron_expression": "0 9 * * *",
        "prompt": "report",
    }
    path = "/api/admin/cron-jobs"
    for hook in [
        "https://example.com/?key=secret",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=",
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
    ]:
        result = await client.post(
            path, json={**body, "notify_webhook": True, "notify_webhook_url": hook}
        )
        assert result.status_code == 422
        assert hook not in result.text
    assert (await client.post(path, json={**body, "notify_webhook": True})).status_code == 422
    result = await client.post(path, json=body)
    assert result.status_code == 201 and not result.json()["data"]["notify_webhook"]
    row = result.json()["data"]
    assert (
        await client.post(
            f"{path}/{row['id']}/test-notification", headers={"If-Match": f'"{row["version"]}"'}
        )
    ).status_code == 422


async def test_migration_preserves_existing_job_destinations(
    client: httpx.AsyncClient, db_session: AsyncSession, db_engine, app
) -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from coreman.core.db.models import User

    bot, _, _ = await seed_bot(db_session)
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=legacy-synthetic"
    bot.notify_webhook_url = hook
    creator = await db_session.get(User, bot.created_by)
    db_session.add(
        UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="legacy-owner")
    )
    db_session.add(UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    response = await client.post(
        "/api/admin/cron-jobs",
        json={
            "bot_id": str(bot.id),
            "name": "迁移",
            "cron_expression": "0 9 * * *",
            "prompt": "report",
        },
    )
    assert response.status_code == 201
    job_id = response.json()["data"]["id"]

    def migrate(connection):
        # 在同一事务中重现上一版字段，再执行真实升级函数。
        connection.execute(text("ALTER TABLE cron_jobs DROP COLUMN notify_webhook_url_enc"))
        connection.execute(
            text("UPDATE cron_jobs SET notify_webhook=true WHERE id=:id"), {"id": job_id}
        )
        migration = importlib.import_module("migrations.versions.0020_cron_notification_webhook")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        return connection.execute(
            text("SELECT notify_webhook, notify_webhook_url_enc FROM cron_jobs WHERE id=:id"),
            {"id": job_id},
        ).one()

    async with db_engine.begin() as connection:
        enabled, encrypted = await connection.run_sync(migrate)
    assert enabled and encrypted != hook
    assert app.state.cipher.decrypt(encrypted, "notifications.webhook_url") == hook


async def test_once_validation_consumption_edit_and_explicit_reschedule(client, db_session):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from coreman.core.db.models import User, UserReached

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add_all(
        [
            UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="owner"),
            UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"),
        ]
    )
    await db_session.commit()
    await login_existing(client, db_session, creator)
    path = "/api/admin/cron-jobs"
    now = datetime.now(UTC)
    body = {
        "bot_id": str(bot.id),
        "name": "once",
        "prompt": "report",
        "schedule_kind": "once",
        "run_at": (now + timedelta(hours=2)).isoformat(),
    }
    assert (await client.post(path, json={**body, "run_at": now.isoformat()})).status_code == 422
    response = await client.post(path, json=body)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["schedule_kind"] == "once" and data["run_at"] == data["next_run_at"]
    row = await db_session.get(CronJob, UUID(data["id"]))
    row.consumed_at, row.next_run_at, row.enabled = now, None, False
    row.run_at = now - timedelta(hours=2)
    body["run_at"] = row.run_at.isoformat()
    await db_session.commit()
    headers = {"If-Match": f'"{row.version}"'}
    response = await client.put(
        f"{path}/{row.id}", json={**body, "name": "renamed", "enabled": False}, headers=headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["consumed_at"] and data["next_run_at"] is None
    headers = {"If-Match": f'"{data["version"]}"'}
    assert (await client.put(f"{path}/{row.id}", json=body, headers=headers)).status_code == 422
    assert (await client.post(f"{path}/{row.id}/run", headers=headers)).status_code == 409
    response = await client.put(
        f"{path}/{row.id}",
        json={**body, "run_at": (now + timedelta(hours=3)).isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["consumed_at"] is None


async def test_active_recipient_reachability_and_disabled_history(client, db_session):
    from coreman.core.db.models import OutboxItem, User, UserReached

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="owner"))
    await db_session.commit()
    await login_existing(client, db_session, creator)
    path = "/api/admin/cron-jobs"
    body = {
        "bot_id": str(bot.id),
        "name": "unreachable",
        "prompt": "report",
        "cron_expression": "0 9 * * *",
    }
    response = await client.post(path, json=body)
    assert response.status_code == 422 and "私聊" in response.text
    response = await client.post(path, json={**body, "enabled": False})
    assert response.status_code == 201, response.text
    assert await db_session.scalar(select(OutboxItem)) is None
    db_session.add(UserReached(bot_id=bot.id, user_id=creator.id, platform_chat_id="private"))
    await db_session.commit()
    assert (await client.post(path, json=body)).status_code == 201


async def test_list_reports_latest_actual_delivery_and_historical_errors(client, db_session):
    from datetime import UTC, datetime

    from coreman.core.db.models import CronRun, OutboxItem, User

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="owner"))
    row = CronJob(
        bot_id=bot.id,
        created_by=creator.id,
        name="legacy",
        prompt="report",
        cron_expression="0 9 * * *",
        last_status="success",
    )
    db_session.add(row)
    await db_session.flush()
    run = CronRun(
        cron_job_id=row.id,
        bot_id=bot.id,
        job_name=row.name,
        prompt=row.prompt,
        status="success",
        started_at=datetime.now(UTC),
        delivery={"errors": {"user:x": "recipient_unbound"}},
    )
    db_session.add(run)
    await db_session.commit()
    await login_existing(client, db_session, creator)
    response = await client.get("/api/admin/cron-jobs")
    data = response.json()["data"]["items"][0]
    assert data["last_status"] == "success" and data["delivery_status"] == "failed"
    item = OutboxItem(
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        target={"chat_id": "private"},
        payload={},
        status="sent",
        dedupe_key="test:summary",
    )
    db_session.add(item)
    await db_session.flush()
    run.delivery = {"errors": {"user:x": "recipient_unbound"}, "outbox_ids": [item.id]}
    await db_session.commit()
    response = await client.get("/api/admin/cron-jobs")
    assert response.json()["data"]["items"][0]["delivery_status"] == "mixed"
    await db_session.refresh(row)
    assert row.enabled  # Reading never silently changes legacy schedules.


async def test_save_uses_same_unique_notification_app_rules_as_delivery(client, db_session, app):
    from coreman.core.cron.delivery import enqueue_result
    from coreman.core.db.models import OutboxItem, PlatformApp, User

    bot, _, _ = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="owner"))
    notify = PlatformApp(
        platform="wecom", name="notify", capabilities=["notify"], secret_enc="unused"
    )
    db_session.add(notify)
    await db_session.commit()
    await login_existing(client, db_session, creator)
    body = {
        "bot_id": str(bot.id),
        "name": "reachable via app",
        "prompt": "report",
        "cron_expression": "0 9 * * *",
    }
    assert (await client.post("/api/admin/cron-jobs", json=body)).status_code == 201
    assert await db_session.scalar(select(OutboxItem)) is None
    delivery = await enqueue_result(
        db_session,
        bot=bot,
        config={},
        run_id="parity",
        content="result",
        cipher=app.state.cipher,
        fallback_user_id=creator.id,
    )
    assert not delivery["errors"] and delivery["outbox_ids"]
    db_session.add(
        PlatformApp(
            platform="wecom", name="ambiguous", capabilities=["notify"], secret_enc="unused"
        )
    )
    await db_session.commit()
    assert (await client.post("/api/admin/cron-jobs", json=body)).status_code == 422
    delivery = await enqueue_result(
        db_session,
        bot=bot,
        config={},
        run_id="ambiguous",
        content="result",
        cipher=app.state.cipher,
        fallback_user_id=creator.id,
    )
    assert (
        not delivery["outbox_ids"]
        and "no_private_chat_or_unambiguous_notification_app" in delivery["errors"].values()
    )
