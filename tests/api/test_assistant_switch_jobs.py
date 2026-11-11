"""切换作业接口会返回任务的结果与报错：助理记录的只有所有者能看，其他人一律 404。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, Task, User
from tests.api.conftest import login_as, login_existing
from tests.integration.assistant_helpers import MASTER_KEY, SECRET, raw, seed_assistant
from tests.integration.worker_helpers import seed_bot


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


async def _finished_job(session: AsyncSession, bot: Bot) -> Task:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="relay_switch", payload={"origin": "admin"})
    )
    assert task is not None
    await tasks.finish(
        session,
        task.id,
        status="succeeded",
        result={"ok": False, "message": f"目录里有 {SECRET}"},
        bot_id=bot.id,
    )
    await session.commit()
    return task


async def test_only_the_owner_reads_an_assistant_switch_job(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task = await _finished_job(db_session, bot)
    assert SECRET not in str(
        await raw(db_session, "SELECT result::text FROM tasks WHERE id = :id", id=task.id)
    )
    url = f"/api/admin/bots/{bot.id}/switch-jobs/{task.id}"
    await login_as(client, db_session, role="platform_admin")
    for response in (await client.get(url), await client.post(url + "/cancel")):
        assert response.status_code == 404 and SECRET not in response.text
        assert "机器人不存在" in response.text
    # 与真的不存在是同一个回答。
    missing = await client.get(url.replace(str(bot.id), "00000000-0000-4000-8000-000000000000"))
    assert missing.status_code == 404 and missing.json() == response.json()
    await login_existing(client, db_session, owner)
    mine = await client.get(url)
    assert mine.status_code == 200, mine.text
    assert mine.json()["data"]["result"] == {"ok": False, "message": f"目录里有 {SECRET}"}
    assert (await client.post(url + "/cancel")).status_code == 200


async def test_standard_bot_switch_job_is_still_open_to_admins(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    task = await _finished_job(db_session, bot)
    url = f"/api/admin/bots/{bot.id}/switch-jobs/{task.id}"
    await login_as(client, db_session, role="platform_admin")
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"]["message"] == f"目录里有 {SECRET}"
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    assert (await client.get(url)).status_code == 200
