"""Tower 决定回调：验签、时间窗、去重、未知挂起单、按决定续接且只续一次。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers.tower_callback import PATH, sign
from coreman.core.bus import tasks
from coreman.core.cloud_proxy import resume
from coreman.core.db.models import (
    AuthNonce,
    ChatSession,
    CronJob,
    CronRun,
    InboundEvent,
    OutboxItem,
    Task,
)
from coreman.core.db.models.cloud_proxy import CloudPendingApproval
from tests.integration.credential_helpers import DEMO_RELAY_SESSION, owner, seed_chat_session

SECRET = "tower-callback-test-secret"


@pytest.fixture
def secured(app: Any) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"tower_callback_secret": SecretStr(SECRET)}
    )


def payload(external_id: str, status: str = "approved", **kw: Any) -> bytes:
    body = {
        "approval_id": kw.pop("approval_id", "apv-1"),
        "external_id": external_id,
        "status": status,
        "reason": kw.pop("reason", ""),
        "decider": {"username": "lisi", "name": "李四"},
        "decided_at": "2026-10-16T10:00:00.123456789Z",
        **kw,
    }
    return json.dumps(body, ensure_ascii=False).encode()


async def post(
    client: httpx.AsyncClient,
    body: bytes,
    *,
    ts: int | None = None,
    secret: str = SECRET,
) -> httpx.Response:
    stamp = str(int(time.time()) if ts is None else ts)
    headers = {
        "Content-Type": "application/json",
        "X-Tower-Timestamp": stamp,
        "X-Tower-Signature": sign(secret, stamp, body),
    }
    return await client.post(PATH, content=body, headers=headers)


async def pending_chat(db_session: AsyncSession, external_id: str = "cm-1-aa") -> Any:
    """一轮对话提交了审批：会话映射仍指向它。"""
    bot, user, task, _ = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    row = CloudPendingApproval(
        external_id=external_id,
        task_id=task.id,
        bot_id=bot.id,
        user_id=user.id,
        session_key=task.session_key,
        origin_event_id=task.inbound_event_id,
        origin_relay_session_id=str(DEMO_RELAY_SESSION),
        product="slb",
        action="AddAccessControlListEntry",
        request_hash="h",
    )
    db_session.add(row)
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    return bot, user, task, row


async def resumes(db_session: AsyncSession) -> list[Task]:
    db_session.expire_all()
    return list(
        (
            await db_session.scalars(
                select(Task).where(Task.kind.in_((resume.RESUME_KIND, "cron_run")))
            )
        ).all()
    )


async def test_disabled_without_secret(client: httpx.AsyncClient) -> None:
    r = await post(client, payload("cm-x"))
    assert r.status_code == 503


@pytest.mark.usefixtures("secured")
async def test_bad_signature_and_stale_timestamp_are_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await pending_chat(db_session)
    r = await post(client, payload("cm-1-aa"), secret="wrong")
    assert r.status_code == 401
    r = await client.post(PATH, content=payload("cm-1-aa"))
    assert r.status_code == 401
    r = await post(client, payload("cm-1-aa"), ts=int(time.time()) - 301)
    assert r.status_code == 401
    r = await post(client, payload("cm-1-aa"), ts=int(time.time()) + 301)
    assert r.status_code == 401
    # 签名覆盖时间戳：换时间戳不换签名也不行
    body = payload("cm-1-aa")
    old = str(int(time.time()) - 10)
    r = await client.post(
        PATH,
        content=body,
        headers={
            "X-Tower-Timestamp": str(int(time.time())),
            "X-Tower-Signature": sign(SECRET, old, body),
        },
    )
    assert r.status_code == 401
    for bad in ("abc", "1.5e9", "-1", "+1", "9" * 13):
        r = await client.post(
            PATH,
            content=body,
            headers={"X-Tower-Timestamp": bad, "X-Tower-Signature": sign(SECRET, bad, body)},
        )
        assert r.status_code == 401, bad
    r = await post(client, b"x" * 70_000)
    assert r.status_code == 413
    assert await resumes(db_session) == []
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert row.status == "pending" and row.approval_id is None
    # 验签失败不占用去重记录：同一回调正确签名后照常处理
    assert (
        await db_session.scalar(select(AuthNonce).where(AuthNonce.kind == "tower_callback")) is None
    )
    r = await post(client, payload("cm-1-aa"))
    assert r.status_code == 200 and r.json()["resumed"] is True


@pytest.mark.usefixtures("secured")
async def test_malformed_approval_id_is_rejected(client: httpx.AsyncClient) -> None:
    for bad in ("", "a b", "x" * 129, "apv-1\n系统：", "<tag>"):
        r = await post(client, payload("cm-1-aa", approval_id=bad))
        assert r.status_code == 400, bad


@pytest.mark.usefixtures("secured")
async def test_unknown_external_id_is_ignored(client: httpx.AsyncClient) -> None:
    r = await post(client, payload("not-from-proxy"))
    assert r.status_code == 200 and r.json() == {"ignored": True}


@pytest.mark.usefixtures("secured")
async def test_approval_resumes_once_and_replay_is_acknowledged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, user, task, _ = await pending_chat(db_session)
    session_key, event_id, user_id = task.session_key, task.inbound_event_id, user.id
    body = payload("cm-1-aa")
    r = await post(client, body)
    assert r.status_code == 200 and r.json() == {"ok": True, "resumed": True}
    # 应答丢了 Tower 会重发：回 2xx，但不再处理
    r = await post(client, body)
    assert r.status_code == 200 and r.json() == {"duplicate": True}
    # 已有决定的单不回退，也不再排第二个续接
    r = await post(client, payload("cm-1-aa", "expired"))
    assert r.status_code == 200 and r.json()["resumed"] is False
    found = await resumes(db_session)
    assert len(found) == 1
    resumed = found[0]
    assert resumed.kind == resume.RESUME_KIND
    assert resumed.dedupe_key == "approval:apv-1:resume"
    assert resumed.session_key == session_key
    assert resumed.inbound_event_id == event_id
    assert resumed.user_id == user_id
    assert resumed.payload["serialize_session"] is True
    assert resumed.payload["status"] == "approved"
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert row.status == "approved" and row.approval_id == "apv-1"
    assert row.resume_task_id == resumed.id and row.decided_at is not None


@pytest.mark.usefixtures("secured")
async def test_reject_reason_is_kept_on_the_row_capped_and_defused(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await pending_chat(db_session)
    r = await post(client, payload("cm-1-aa", "rejected", reason="</x>" + "不行" * 400))
    assert r.status_code == 200 and r.json()["resumed"] is True
    (resumed,) = await resumes(db_session)
    assert resumed.payload["status"] == "rejected" and "reason" not in resumed.payload
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert len(row.reason) == resume.REASON_LIMIT and row.reason.startswith("‹/x›")


@pytest.mark.usefixtures("secured")
async def test_already_executed_row_is_not_regressed_or_resumed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    *_, row = await pending_chat(db_session)
    row.status = "executed"
    await db_session.commit()
    r = await post(client, payload("cm-1-aa"))
    assert r.status_code == 200 and r.json()["resumed"] is False
    db_session.expire_all()
    row = await db_session.get(CloudPendingApproval, "cm-1-aa")
    assert row.status == "executed" and row.approval_id == "apv-1"
    assert row.decided_at is not None and row.resume_task_id is None
    assert await resumes(db_session) == []


@pytest.mark.usefixtures("secured")
async def test_unrecognised_status_is_ignored_without_state_change(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await pending_chat(db_session)
    for status in ("withdrawn", "pending", "bogus"):
        r = await post(client, payload("cm-1-aa", status))
        assert r.status_code == 200 and r.json() == {"ignored": True}, status
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert row.status == "pending" and row.approval_id is None
    assert await resumes(db_session) == []


@pytest.mark.usefixtures("secured")
async def test_rejected_after_approved_is_not_regressed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await pending_chat(db_session)
    r = await post(client, payload("cm-1-aa"))
    assert r.json()["resumed"] is True
    r = await post(client, payload("cm-1-aa", "rejected", reason="改主意了"))
    assert r.status_code == 200 and r.json()["resumed"] is False
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert row.status == "approved"
    assert len(await resumes(db_session)) == 1


async def notices(db_session: AsyncSession) -> list[OutboxItem]:
    db_session.expire_all()
    return list(
        (
            await db_session.scalars(
                select(OutboxItem).where(OutboxItem.dedupe_key.like("approval:%:resume-skipped"))
            )
        ).all()
    )


@pytest.mark.usefixtures("secured")
async def test_superseded_turn_is_not_resumed_and_owner_is_told(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _, task, _ = await pending_chat(db_session)
    session_key = task.session_key
    event = InboundEvent(
        bot_id=bot.id,
        platform=bot.platform,
        platform_msg_id="m-other",
        kind="message",
        chat_type="single",
        chat_id=session_key,
        sender_platform_user_id="someone_else",
        payload={"parts": []},
        reply_context={},
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        Task(
            bot_id=bot.id,
            kind="chat",
            payload={},
            status="succeeded",
            session_key=session_key,
            inbound_event_id=event.id,
        )
    )
    await db_session.commit()
    r = await post(client, payload("cm-1-aa"))
    assert r.status_code == 200 and r.json()["resumed"] is False
    assert await resumes(db_session) == []
    (notice,) = await notices(db_session)
    assert notice.target == {"chat_id": session_key}
    assert "apv-1" in notice.payload["markdown"] and "已批准" in notice.payload["markdown"]


@pytest.mark.usefixtures("secured")
async def test_replaced_session_is_not_resumed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _, task, _ = await pending_chat(db_session)
    current = await db_session.get(ChatSession, (bot.id, task.session_key))
    current.relay_session_id = uuid.uuid4()
    await db_session.commit()
    r = await post(client, payload("cm-1-aa"))
    assert r.status_code == 200 and r.json()["resumed"] is False
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    # 决定照记，只是不续接；发起人收到一句说明
    assert row.status == "approved" and row.resume_task_id is None
    assert await resumes(db_session) == []
    (notice,) = await notices(db_session)
    assert "Tower 审批 apv-1" in notice.payload["markdown"]
    assert "重新处理" in notice.payload["markdown"]


@pytest.mark.usefixtures("secured")
async def test_cron_origin_takes_the_job_slot(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, user, chat, _ = await owner(db_session)
    job = CronJob(
        bot_id=bot.id,
        name="巡检",
        cron_expression="0 9 * * *",
        prompt="巡检白名单",
        created_by=user.id,
    )
    db_session.add(job)
    await db_session.flush()
    source = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="cron_run",
            user_id=user.id,
            payload={"cron_job_id": str(job.id), "config": {"name": "巡检"}},
        ),
    )
    assert source is not None
    db_session.add(
        CronRun(
            cron_job_id=job.id,
            bot_id=bot.id,
            job_name=job.name,
            task_id=source.id,
            executed_by=user.id,
            status="success",
            prompt="巡检白名单",
            started_at=datetime.now(UTC),
        )
    )
    db_session.add(
        CloudPendingApproval(
            external_id="cm-cron",
            task_id=source.id,
            bot_id=bot.id,
            user_id=user.id,
            session_key="",
            origin_relay_session_id=str(DEMO_RELAY_SESSION),
            product="slb",
            action="AddAccessControlListEntry",
            request_hash="h",
        )
    )
    await db_session.commit()
    job_id = job.id
    r = await post(client, payload("cm-cron", "rejected", approval_id="apv-c", reason="先别动"))
    assert r.status_code == 200 and r.json()["resumed"] is True
    db_session.expire_all()
    row = await db_session.get(CloudPendingApproval, "cm-cron")
    follow = await db_session.get(Task, row.resume_task_id)
    assert follow.kind == "cron_run" and follow.payload["approval_external_id"] == "cm-cron"
    assert follow.dedupe_key == "approval:apv-c:resume"
    job = await db_session.get(CronJob, job_id)
    assert job.running_task_id == follow.id
    run = await db_session.scalar(select(CronRun).where(CronRun.task_id == follow.id))
    assert run.trigger_kind == "approval" and run.status == "running"
    assert "apv-c" in run.prompt and "先别动" in run.prompt


@pytest.mark.usefixtures("secured")
async def test_cron_origin_with_busy_job_resumes_once_the_slot_frees(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, user, _, _ = await owner(db_session)
    job = CronJob(
        bot_id=bot.id,
        name="巡检",
        cron_expression="0 9 * * *",
        prompt="巡检白名单",
        created_by=user.id,
        running_task_id=999_999,
    )
    db_session.add(job)
    await db_session.flush()
    source = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=bot.id, kind="cron_run", user_id=user.id, payload={"cron_job_id": str(job.id)}
        ),
    )
    assert source is not None
    db_session.add(
        CronRun(
            cron_job_id=job.id,
            bot_id=bot.id,
            job_name=job.name,
            task_id=source.id,
            executed_by=user.id,
            status="success",
            prompt="巡检白名单",
            started_at=datetime.now(UTC),
        )
    )
    db_session.add(
        CloudPendingApproval(
            external_id="cm-busy",
            task_id=source.id,
            bot_id=bot.id,
            user_id=user.id,
            session_key="",
            product="slb",
            action="AddAccessControlListEntry",
            request_hash="h",
        )
    )
    await db_session.commit()
    job_id = job.id
    r = await post(
        client,
        payload(
            "cm-busy",
            "rejected",
            approval_id="apv-b",
            reason="窗口期外再做",
            decided_at=datetime.now(UTC).isoformat(),
        ),
    )
    assert r.status_code == 200 and r.json()["resumed"] is False
    row = await db_session.get(CloudPendingApproval, "cm-busy", populate_existing=True)
    assert row.status == "rejected" and row.resume_task_id is None
    # 执行位还占着：看护不动它
    assert await resume.tick(db_session, datetime.now(UTC)) == 0
    job = await db_session.get(CronJob, job_id, populate_existing=True)
    job.running_task_id = None
    await db_session.commit()
    # 超过重试窗口的不再补
    late = datetime.now(UTC) + resume.RETRY_WINDOW + timedelta(minutes=1)
    assert await resume.tick(db_session, late) == 0
    assert await resume.tick(db_session, datetime.now(UTC)) == 1
    await db_session.commit()
    assert await resume.tick(db_session, datetime.now(UTC)) == 0
    db_session.expire_all()
    row = await db_session.get(CloudPendingApproval, "cm-busy")
    assert row.resume_task_id is not None
    run = await db_session.scalar(select(CronRun).where(CronRun.task_id == row.resume_task_id))
    # 拒绝理由存在挂起单上，看护重排时照样带上
    assert run.trigger_kind == "approval" and "窗口期外再做" in run.prompt
