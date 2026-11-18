"""The console's view of batch writes: recent batches as metadata, and stopping one."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from coreman.core.db.models import AuditLog, BotMember, User
from coreman.core.systems_catalog import batch, confirm
from tests.api import test_systems_batch_writes as writes
from tests.api.conftest import login_as, login_existing

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
    # 和这个员工、这个批次都无关的人看不到。
    await login_as(client, db_session, role="member")
    assert await listed(client) == []
    assert (await client.get(f"{URL}/{state.id}/items")).status_code == 404
    await login_as(client, db_session, role="ai_committee")

    (row,) = await listed(client)
    assert row["id"] == str(state.id) and row["phase"] == "running"
    assert (row["system"], row["operation_id"], row["count"]) == ("stock", "createDraft", 12)
    assert row["bot_name"] and row["user_name"] and row["pending"] == 12
    assert row["can_stop"] is True
    # The list carries metadata only; the staged requests are in the item view.
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


async def test_the_asker_and_the_employees_members_see_it_and_only_the_asker_can_stop(
    client,
    app,
    db_session,
    db_engine,
    fake,
    monkeypatch,
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, task, state = await confirmed(client, app, db_session, db_engine, "A", "B", "C")
    asker = await db_session.get(User, uuid.UUID(state.state["user_id"]))
    assert asker is not None
    member = await login_as(client, db_session, role="member")
    db_session.add(BotMember(bot_id=bot.id, user_id=member.id, role="admin"))
    await db_session.commit()
    (row,) = await listed(client)
    assert row["id"] == str(state.id) and row["can_stop"] is False
    assert (await client.post(f"{URL}/{state.id}/stop")).status_code == 403
    # 成员看别人提交的明细：记审计。
    assert (await client.get(f"{URL}/{state.id}/items")).status_code == 200
    viewed = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "system_batch.view_items")
    )
    assert viewed is not None and viewed.actor_id == member.id

    await login_existing(client, db_session, asker)
    (row,) = await listed(client)
    assert row["can_stop"] is True
    assert await run_next(db_session, db_engine)
    stopped = await client.post(f"{URL}/{state.id}/stop")
    assert stopped.status_code == 200 and stopped.json()["data"]["stopping"] is True
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped", "skipped"]
    final = text_of((await updates(db_session))[-1])
    assert "按发起人的要求停止" in final and "管理员" not in final
    # 发起人看自己的明细不记审计。
    assert (await client.get(f"{URL}/{state.id}/items")).status_code == 200
    views = await db_session.scalars(
        select(AuditLog).where(AuditLog.action == "system_batch.view_items")
    )
    assert len(list(views)) == 1


async def test_items_show_what_was_sent_and_how_it_went_page_by_page(
    client,
    app,
    db_session,
    db_engine,
    fake,
) -> None:
    bot, task, state = await confirmed(
        client, app, db_session, db_engine, "SKU-1", "BAD-2", "SKU-3"
    )
    await run_all(db_session, db_engine)
    await login_as(client, db_session, role="platform_admin")
    response = await client.get(f"{URL}/{state.id}/items", params={"per_page": 2})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 3 and [item["seq"] for item in data["items"]] == [1, 2]
    first, second = data["items"]
    assert first["body"]["sku"] == "SKU-1" and first["status"] == "ok"
    assert first["status_code"] == 200 and first["finished_at"]
    assert second["body"]["sku"] == "BAD-2" and second["status"] == "http_error"
    assert second["brief"]
    failed = await client.get(f"{URL}/{state.id}/items", params={"status": "failed"})
    assert [item["seq"] for item in failed.json()["data"]["items"]] == [2]
    page2 = await client.get(f"{URL}/{state.id}/items", params={"per_page": 2, "page": 2})
    assert [item["seq"] for item in page2.json()["data"]["items"]] == [3]
    assert (
        await client.get(f"{URL}/{state.id}/items", params={"status": "nope"})
    ).status_code == 422
