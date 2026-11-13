"""「我的助理」页的子任务：所有者看到交代原文与进度，可以取消进行中的；别人碰不到。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs, keys, sealed
from coreman.core.db.models import Bot, User
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token, code
from tests.integration.assistant_helpers import SECRET, raw, seed_standard
from tests.integration.job_helpers import owner_with_key, reloaded, spawned

pytestmark = pytest.mark.usefixtures("assistant_master")
JOBS = f"{URL}/jobs"
BRIEF = f"查一下上季度的退货原因。{SECRET}"
STORED = (
    "SELECT string_agg(title_enc || brief_enc, chr(32)) FROM assistant_jobs WHERE bot_id = :bot"
)


async def _owner(client: httpx.AsyncClient, db_session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


async def test_the_owner_sees_running_and_finished_jobs_with_the_brief(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    running = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    ended = await spawned(db_session, bot, title="已取消的调查", brief="不用查了")
    await jobs.cancel(db_session, bot.id, ended.id)
    await db_session.commit()
    response = await client.get(JOBS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    [active] = data["active"]
    assert active["id"] == str(running.id) and active["status"] == "queued"
    assert active["title"] == "退货原因" and active["brief"] == BRIEF
    [recent] = data["recent"]
    assert recent["id"] == str(ended.id) and recent["status"] == "cancelled"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))


async def test_the_owner_cancels_a_running_job_and_the_assistant_is_told(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    response = await client.post(f"{JOBS}/{row.id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"id": str(row.id), "status": "cancelled"}
    row = await reloaded(db_session, row)
    assert row.status == "cancelled" and row.error_code == "cancelled_by_owner"
    # 要投回主会话：没有被记成「不需要投回」。
    assert row.report_code != "not_needed"
    again = await client.post(f"{JOBS}/{row.id}/cancel")
    assert again.status_code == 409 and code(again) == "job_closed"
    missing = await client.post(f"{JOBS}/{uuid.uuid4()}/cancel")
    assert missing.status_code == 404 and code(missing) == "job_not_found"
    assert (await client.post(f"{JOBS}/not-a-uuid/cancel")).status_code == 422


async def test_cancel_needs_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await _owner(client, db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post(f"{JOBS}/{row.id}/cancel")).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(JOBS)).status_code == 403
    assert (await client.post(f"{JOBS}/{row.id}/cancel")).status_code == 403
    assert (await reloaded(db_session, row)).status == "queued"


async def test_an_admin_with_their_own_assistant_cannot_see_or_cancel_the_owners_jobs(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await owner_with_key(db_session)
    row = await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    _theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    listed = await client.get(JOBS)
    assert listed.json()["data"] == {"active": [], "recent": []} and SECRET not in listed.text
    refused = await client.post(f"{JOBS}/{row.id}/cancel")
    assert refused.status_code == 404 and code(refused) == "job_not_found"
    assert (await reloaded(db_session, row)).status == "queued"


async def test_after_the_key_is_destroyed_jobs_show_as_locked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await _owner(client, db_session)
    await spawned(db_session, bot, title="退货原因", brief=BRIEF)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    sealed.forget()
    response = await client.get(JOBS)
    assert response.status_code == 200 and SECRET not in response.text
    [active] = response.json()["data"]["active"]
    assert active["locked"] is True and active["title"] == "" and active["brief"] == ""


async def test_dispatches_and_platform_jobs_are_told_apart(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """调度与子任务同表：按 `kind` 区分、带目标员工名；平台派的按 `origin` 区分。"""
    bot, _owner_row = await _owner(client, db_session)
    target = await seed_standard(db_session)
    asked = await spawned(db_session, bot, title="请「demo-bot」协助", brief=BRIEF)
    asked.kind, asked.target_bot_id = "bot_help", target.id
    compiling = await spawned(
        db_session, bot, title="下班整理", brief="整理今天的活动", origin="system", report=False
    )
    mine = await spawned(db_session, bot, title="退货原因", brief="查一下")
    await db_session.commit()
    active = {item["id"]: item for item in (await client.get(JOBS)).json()["data"]["active"]}
    assert set(active) == {str(asked.id), str(compiling.id), str(mine.id)}
    assert active[str(asked.id)]["kind"] == "bot_help"
    assert active[str(asked.id)]["target"] == {"id": str(target.id), "name": "demo-bot"}
    assert active[str(asked.id)]["origin"] == "assistant"
    assert active[str(compiling.id)]["origin"] == "system"
    assert active[str(compiling.id)]["kind"] == "subtask"
    assert active[str(compiling.id)]["target"] is None
    assert active[str(mine.id)]["origin"] == "assistant" and active[str(mine.id)]["target"] is None
    # 调度与子任务用同一个取消接口。
    cancelled = await client.post(f"{JOBS}/{asked.id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert (await reloaded(db_session, asked)).error_code == "cancelled_by_owner"
