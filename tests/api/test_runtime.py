from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import instances, leases, outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import AuditLog, Bot, BotLease, OutboxItem, ProcessInstance, Task, User
from tests.api.conftest import login_as


async def _seed(db_session: AsyncSession) -> Bot:
    u = User(login_name="c", display_name="c")
    db_session.add(u)
    await db_session.flush()
    bot = Bot(
        bot_key="bb",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    db_session.add(bot)
    await db_session.flush()
    gw = await instances.register(
        db_session,
        instance_id="gateway-wecom-a:h:1:1",
        service="gateway-wecom",
        version="dev",
        capacity=None,
    )
    w = await instances.register(
        db_session, instance_id="worker-a:h:2:1", service="worker", version="dev", capacity=30
    )
    await leases.ensure_rows(db_session, "wecom")
    assert await leases.acquire(db_session, bot_id=bot.id, platform="wecom", instance_id=gw.id)
    t = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="s")
    )
    assert t and await tasks.claim(db_session, lane="normal", instance_id=w.id)
    await tasks.start(db_session, t.id)
    await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="command", lane="fast", payload={}, session_key="s2"),
    )
    item = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="wecom",
        kind="send",
        dedupe_key="k",
        target={},
        payload={},
    )
    assert item
    for _ in range(6):
        await outbox.mark_failed(db_session, item.id, "boom")
    await db_session.commit()
    return bot


async def test_read_endpoints_and_roles(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await _seed(db_session)
    await login_as(client, db_session, role="member")
    assert (await client.get("/api/admin/runtime/queue")).status_code == 403
    await login_as(client, db_session, role="team_lead")
    q = (await client.get("/api/admin/runtime/queue")).json()["data"]
    assert q == {
        "queued": {"normal": 0, "fast": 1},
        "claimed": 0,
        "running": 1,
        "outbox_pending": 0,
        "outbox_failed": 1,
        "streams_active": 0,
    }
    inst = (await client.get("/api/admin/runtime/instances")).json()["data"]
    assert {i["service"] for i in inst} == {"gateway-wecom", "worker"} and all(
        i["alive"] for i in inst
    )
    ls = (await client.get("/api/admin/runtime/leases")).json()["data"]
    assert ls[0]["bot_key"] == "bb" and ls[0]["generation"] == 1
    assert ls[0]["holder_instance"].startswith("gateway-wecom-a")
    active = (await client.get("/api/admin/runtime/tasks", params={"status": "active"})).json()[
        "data"
    ]
    assert len(active) == 1 and active[0]["status"] == "running" and active[0]["bot_key"] == "bb"
    failed = (await client.get("/api/admin/runtime/outbox", params={"status": "failed"})).json()[
        "data"
    ]
    assert len(failed) == 1 and failed[0]["attempts"] == 6
    assert (
        await client.post(f"/api/admin/runtime/outbox/{failed[0]['id']}/retry")
    ).status_code == 403
    assert bot.id


async def test_write_endpoints_platform_admin(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed(db_session)
    failed = (
        await admin_client.get("/api/admin/runtime/outbox", params={"status": "failed"})
    ).json()["data"]
    r = await admin_client.post(f"/api/admin/runtime/outbox/{failed[0]['id']}/retry")
    assert r.status_code == 200
    assert (
        await admin_client.post(f"/api/admin/runtime/outbox/{failed[0]['id']}/retry")
    ).status_code == 409
    assert (await admin_client.post("/api/admin/runtime/outbox/999999/retry")).status_code == 404
    running = (
        await admin_client.get("/api/admin/runtime/tasks", params={"status": "active"})
    ).json()["data"][0]
    r = await admin_client.post(
        f"/api/admin/runtime/tasks/{running['id']}/cancel", json={"reason": "admin"}
    )
    assert r.status_code == 200
    row = await tasks.get(db_session, running["id"])
    assert row and row.cancel_reason == "admin"
    assert (await admin_client.post("/api/admin/runtime/drain", json={})).status_code == 422
    assert (
        await admin_client.post("/api/admin/runtime/drain", json={"instance_id": "nope"})
    ).status_code == 404
    assert (
        await admin_client.post("/api/admin/runtime/drain", json={"instance_id": "worker-a:h:2:1"})
    ).status_code == 200
    assert (
        await admin_client.post("/api/admin/runtime/drain", json={"bot_key": "bb"})
    ).status_code == 200
    inst = (
        await db_session.execute(
            select(ProcessInstance).where(ProcessInstance.id == "worker-a:h:2:1")
        )
    ).scalar_one()
    assert inst.drain_requested_at is not None
    lease = (await db_session.execute(select(BotLease))).scalar_one()
    await db_session.refresh(lease)
    assert lease.drain_requested_by
    actions = {a.action for a in (await db_session.execute(select(AuditLog))).scalars()}
    assert {"runtime.outbox_retry", "runtime.task_cancel", "runtime.drain"} <= actions
    ob = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert ob.status == "pending" and ob.attempts == 0
    assert datetime.now(UTC) - timedelta(minutes=1) < inst.heartbeat_at
    assert (await db_session.execute(select(Task))).scalars().all()


async def test_drain_bot_without_a_live_holder_is_409(
    admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """没有在线网关持有它：这个排空请求没人会执行，别让标记白挂在行上。"""
    bot = await _seed(db_session)
    lease = (await db_session.execute(select(BotLease))).scalar_one()
    lease.heartbeat_at = datetime.now(UTC) - timedelta(seconds=31)  # 持有者心跳过期
    await db_session.commit()
    r = await admin_client.post("/api/admin/runtime/drain", json={"bot_key": "bb"})
    assert r.status_code == 409 and r.json()["code"] == 409
    lease = (await db_session.execute(select(BotLease))).scalar_one()
    await db_session.refresh(lease)
    assert lease.drain_requested_by is None
    # 持有者彻底没了（巡检已经释放）也一样
    lease.holder_instance = None
    await db_session.commit()
    assert (
        await admin_client.post("/api/admin/runtime/drain", json={"bot_key": "bb"})
    ).status_code == 409
    assert bot.id
