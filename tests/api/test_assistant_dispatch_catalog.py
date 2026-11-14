"""个人助理调度来的一轮经业务系统目录代理：只读调度写不进去，所有者批准过的才照员工的授权。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.db.models import Task
from tests.api.test_systems_catalog import bearer, call
from tests.api.test_systems_proxy import proxied_turn
from tests.fakes.business_system import FakeBusinessSystem, install

WRITE = {
    "system": "stock",
    "operation_id": "createDraft",
    "body": {"sku": "SKU-9", "quantity": 3},
}
READ = {"system": "stock", "operation_id": "listDocuments", "query": {"status": "confirmed"}}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    install(monkeypatch, system)
    return system


async def _round(session: AsyncSession, bot: Any, **payload: object) -> Task:
    task = Task(bot_id=bot.id, kind=BOT_HELP_KIND, payload=payload, status="running")
    session.add(task)
    await session.commit()
    return task


async def test_a_read_only_dispatch_cannot_write_through_the_proxy(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    # 这位员工对库存系统开了「允许写入」：所有者自己私聊它时写得进去。
    bot, member, chat = await proxied_turn(db_session, write=True)
    posted = len([r for r in fake.requests if r.method == "POST"])
    for payload in ({}, {"allow_write": False}, {"allow_write": "true"}):
        auth = bearer(app, await _round(db_session, bot, **payload), member)
        read = await call(client, auth, "systems_call", READ)
        assert read["status"] == 200
        denied = await call(client, auth, "systems_call", WRITE)
        assert denied["error"] == "write_not_allowed"
        detail = await call(
            client, auth, "systems_describe", {"system": "stock", "operation_id": "createDraft"}
        )
        assert detail["call"]["allowed"] is False
    assert len([r for r in fake.requests if r.method == "POST"]) == posted
    # 所有者在审批系统里批准过的那一轮照员工的授权直接写，不再发确认卡。
    approved = bearer(app, await _round(db_session, bot, allow_write=True), member)
    assert (await call(client, approved, "systems_call", WRITE))["ok"] is True
    assert len([r for r in fake.requests if r.method == "POST"]) == posted + 1
    # 他自己的私聊照员工的授权，但写操作要等确认卡（这里不是飞书消息触发的，发不了卡）。
    own = await call(client, bearer(app, chat, member), "systems_call", WRITE)
    assert own["error"] == "confirmation_unavailable"


def test_only_an_approved_dispatch_skips_the_card() -> None:
    from coreman.core.systems_catalog import policy

    assert policy.writes_preapproved(Task(kind=BOT_HELP_KIND, payload={"allow_write": True}))
    for kind, payload in (
        (BOT_HELP_KIND, {}),
        (BOT_HELP_KIND, {"allow_write": "true"}),
        ("chat", {"allow_write": True}),
    ):
        assert not policy.writes_preapproved(Task(kind=kind, payload=payload))
