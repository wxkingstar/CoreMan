from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coreman.core.db.models import AlertState
from coreman.core.observability.alerts import observe


async def test_alert_holdoff_and_recovery_survive_transactions(db_session):
    now = datetime.now(UTC)
    assert not await observe(db_session, "queue", True, "alert_queue", now, delay=300)
    await db_session.commit()
    assert not await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=299), delay=300
    )
    assert await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=300), delay=300
    )
    await db_session.commit()
    assert not await observe(
        db_session, "queue", True, "alert_queue", now + timedelta(seconds=330), delay=300
    )
    assert await observe(
        db_session, "queue", False, "alert_queue", now + timedelta(seconds=360), delay=300
    )
    await db_session.commit()
    row = await db_session.scalar(select(AlertState))
    assert not row.active and not row.firing and row.generation == 1


async def test_evaluation_ignores_future_queue_and_detects_enabled_disconnected_bot(db_session):
    from coreman.core.bus import tasks
    from coreman.core.bus.tasks import NewTask
    from coreman.core.observability.alerts import tick
    from tests.integration.test_scheduler_reaper import _bot

    bot = await _bot(db_session)
    now = datetime.now(UTC)
    await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", run_after=now + timedelta(days=1))
    )
    await db_session.commit()
    await tick(db_session, now)
    await db_session.commit()
    queue = await db_session.get(AlertState, "queue")
    gateway = await db_session.get(AlertState, f"gateway:{bot.id}")
    assert not queue.active and gateway.active and not gateway.firing
    await tick(db_session, now + timedelta(seconds=301))
    await db_session.commit()
    await db_session.refresh(gateway)
    assert gateway.firing


async def test_assistants_alert_per_app_connection_not_per_record(db_session):
    from coreman.core.assistants.records import new_assistant_bot
    from coreman.core.bus import app_leases, instances
    from coreman.core.db.models import PlatformApp, User
    from coreman.core.observability.alerts import tick

    owner = User(login_name="zhangsan", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    # 还没填 App ID 的应用网关不会去连，也就不该报断线。
    draft = PlatformApp(
        platform="feishu", name="个人助理（未填）", capabilities=["assistant"], secret_enc="x"
    )
    db_session.add_all([owner, app, draft])
    await db_session.flush()
    await db_session.refresh(app)
    assistant = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
    )
    db_session.add(assistant)
    await db_session.commit()
    now = datetime.now(UTC)
    await tick(db_session, now)
    await db_session.commit()
    assert await db_session.get(AlertState, f"gateway:{assistant.id}") is None
    assert await db_session.get(AlertState, f"gateway:app:{draft.id}") is None
    connection = await db_session.get(AlertState, f"gateway:app:{app.id}")
    assert connection.active and not connection.firing
    # 应用连上之后告警解除。
    await instances.register(
        db_session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await app_leases.ensure_rows(db_session)
    assert await app_leases.acquire(db_session, app.id, "gw-a") == 1
    await app_leases.set_state(db_session, app.id, "subscribed", instance_id="gw-a")
    await db_session.commit()
    await tick(db_session, datetime.now(UTC))
    await db_session.commit()
    await db_session.refresh(connection)
    assert not connection.active
