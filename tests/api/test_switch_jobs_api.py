"""管理台发起的后台切换：入队、当场拒绝、同一员工只能有一个、排队中可取消。"""

import uuid

from sqlalchemy import select

from coreman.core.db.models import Task, User
from tests.api.conftest import login_existing
from tests.fakes.runtime_node import add_node_relay
from tests.integration.worker_helpers import seed_bot


async def test_background_switch_endpoint_queues_one_job(client, db_session):
    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    url = f"/api/admin/bots/{bot.id}/switch-relay?background=true"
    body = {"relay_server_id": str(target.id)}
    first = await client.post(url, json=body, headers={"If-Match": str(bot.version)})
    assert first.status_code == 202, first.text
    task_id = first.json()["data"]["task_id"]
    job = await db_session.get(Task, task_id)
    assert job.kind == "relay_switch" and job.payload["origin"] == "admin"
    assert job.lane == "normal"
    # 同一员工同时只能有一个切换。
    again = await client.post(url, json=body, headers={"If-Match": str(bot.version)})
    assert again.status_code == 409
    status = await client.get(f"/api/admin/bots/{bot.id}/switch-jobs/{task_id}")
    assert status.json()["data"]["status"] == "queued"
    # 还在排队的取消即作废，员工没被动过。
    cancel = await client.post(f"/api/admin/bots/{bot.id}/switch-jobs/{task_id}/cancel")
    assert cancel.status_code == 200
    await db_session.refresh(job)
    assert job.status == "cancelled"
    await db_session.refresh(bot)
    assert bot.workspace_state == "ready"


async def test_background_switch_refuses_unknown_target_on_the_spot(client, db_session):
    bot, _, _ = await seed_bot(db_session)
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay?background=true",
        json={"relay_server_id": str(uuid.uuid4())},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 422
    assert await db_session.scalar(select(Task.id).where(Task.kind == "relay_switch")) is None
