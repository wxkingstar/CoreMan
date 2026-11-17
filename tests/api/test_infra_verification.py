from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import ApiClient, InboundEvent, Task, WorkTask, WorkTaskEvent
from tests.integration.worker_helpers import seed_bot


def signed(path: str, body: dict[str, Any], client: str = "verifier") -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "X-App-Key": client,
        "X-Timestamp": ts,
        "X-Signature": sign_request("POST", path, body, ts, client, "test-secret"),
    }


async def test_verification_failure_joins_original_work_and_replay_is_safe(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _, cipher = await seed_bot(db_session)
    bot.platform = "feishu"
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id="om_original",
        kind="message",
        chat_type="group",
        chat_id="oc_original",
        sender_platform_user_id="ou_requester",
        payload={},
        reply_context={},
    )
    db_session.add(event)
    await db_session.flush()
    work = WorkTask(
        title="修复结账",
        status="running",
        platform="feishu",
        chat_id=event.chat_id,
        origin_event_id=event.id,
        requester_user_id=bot.created_by,
        assignee_bot_id=bot.id,
    )
    db_session.add(work)
    for key, scope in (("verifier", "verification-results"), ("unrelated", "notify")):
        db_session.add(
            ApiClient(
                app_key=key,
                name=key,
                scopes=[scope],
                secret_enc=cipher.encrypt("test-secret", "api_clients.secret_enc"),
            )
        )
    await db_session.commit()
    path = f"/api/infra/work-tasks/{work.code}/verification-results"
    body = {
        "request_id": str(uuid.uuid4()),
        "bot_key": bot.bot_key,
        "project": "looply-web",
        "command": "vitest",
        "sha": "a" * 40,
        "status": "failed",
        "issue_url": "https://multica.example.test/workspace/issues/WEB-123",
        "failures": "src/checkout.test.ts › rejects invalid coupon",
        "log": "AssertionError: expected 400, received 200",
    }
    assert (await client.post(path, json=body)).status_code == 401
    wrong_scope = await client.post(path, json=body, headers=signed(path, body, "unrelated"))
    assert wrong_scope.status_code == 403
    wrong_bot = {**body, "bot_key": "other-bot"}
    assert (
        await client.post(path, json=wrong_bot, headers=signed(path, wrong_bot))
    ).status_code == 409

    first = await client.post(path, json=body, headers=signed(path, body))
    assert first.status_code == 200, first.text
    assert first.json()["data"] == {"work_code": work.code, "replayed": False}
    replay = await client.post(path, json=body, headers=signed(path, body))
    assert replay.json()["data"]["replayed"] is True
    tampered = {**body, "log": "different"}
    assert (await client.post(path, json=tampered, headers=signed(path, body))).status_code == 401
    assert (
        await client.post(path, json=tampered, headers=signed(path, tampered))
    ).status_code == 409

    result = (
        await db_session.scalars(select(WorkTaskEvent).where(WorkTaskEvent.work_task_id == work.id))
    ).one()
    assert result.kind == "verification_result"
    assert result.agent_detail == body
    task = (await db_session.scalars(select(Task).where(Task.bot_id == bot.id))).one()
    assert task.session_key == f"work:{work.code}"
    assert task.inbound_event_id == event.id and task.user_id == work.requester_user_id
    assert task.payload["work_task_id"] == work.id and task.payload["serialize_session"]
    assert body["failures"] in task.payload["work_note"]
