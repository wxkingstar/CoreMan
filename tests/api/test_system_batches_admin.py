"""The console's view of batch writes: recent batches as metadata, and stopping one."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from coreman.core.db.models import AuditLog
from coreman.core.systems_catalog import batch, confirm
from tests.api import test_systems_batch_writes as writes
from tests.api.conftest import login_as

# 复用批量写操作用例的假业务系统与临时存储。
fake = writes.fake
storage = writes.storage
confirmed, run_all, run_next = writes.confirmed, writes.run_all, writes.run_next
statuses, updates, resumed, text_of = (
    writes.statuses,
    writes.updates,
    writes.resumed,
    writes.text_of,
)

URL = "/api/admin/system-batches"


async def listed(client: Any) -> list[dict[str, Any]]:
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["items"])


async def test_the_list_shows_metadata_only_and_follows_the_batch(
    client,
    app,
    db_session,
    db_engine,
    fake,
) -> None:
    skus = [f"SKU-SECRET-{i}" for i in range(12)]
    bot, task, state = await confirmed(client, app, db_session, db_engine, *skus)
    await login_as(client, db_session, role="member")
    assert (await client.get(URL)).status_code == 403
    await login_as(client, db_session, role="ai_committee")

    (row,) = await listed(client)
    assert row["id"] == str(state.id) and row["phase"] == "running"
    assert (row["system"], row["operation_id"], row["count"]) == ("stock", "createDraft", 12)
    assert row["bot_name"] and row["user_name"] and row["pending"] == 12
    # Staged requests are business data: never in the console's view.
    assert "SKU-SECRET" not in str(row)

    await run_all(db_session, db_engine)
    (row,) = await listed(client)
    assert (row["phase"], row["pause_reason"]) == ("paused", batch.PAUSE_TRIAL)
    assert (row["ok"], row["pending"]) == (5, 7)

    # An administrator stops the paused batch: it ends at once and the asker is told who did.
    stopped = await client.post(f"{URL}/{state.id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["phase"] == "finished"
    assert await statuses(db_session) == ["ok"] * 5 + ["skipped"] * 7
    assert "管理员在管理台停止了执行" in text_of((await updates(db_session))[-1])
    (follow,) = await resumed(db_session)
    assert "管理员在管理台停止了执行" in follow.payload[confirm.RESULT_KEY]
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "system_batch.stop"))
    assert audit is not None and audit.target_id == str(state.id)
    assert (await client.post(f"{URL}/{state.id}/stop")).status_code == 409


async def test_stopping_a_running_batch_takes_effect_at_the_next_item(
    client,
    app,
    db_session,
    db_engine,
    fake,
    monkeypatch,
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, task, state = await confirmed(client, app, db_session, db_engine, "A", "B", "C")
    assert await run_next(db_session, db_engine)
    await login_as(client, db_session, role="platform_admin")
    stopped = await client.post(f"{URL}/{state.id}/stop")
    assert stopped.status_code == 200 and stopped.json()["data"]["stopping"] is True
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped", "skipped"]
    assert "管理员在管理台停止了执行" in text_of((await updates(db_session))[-1])
    (row,) = await listed(client)
    assert (row["phase"], row["ok"], row["skipped"]) == ("finished", 1, 2)
    assert (
        await client.post(f"{URL}/00000000-0000-0000-0000-000000000000/stop")
    ).status_code == 404
