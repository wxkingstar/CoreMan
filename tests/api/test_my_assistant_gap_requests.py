"""所有者在页面上：对他开放的系统一键开通（同管理台的写入），白名单外的提交申请、撤回；别人碰不到。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, gaps
from coreman.core.db.models import (
    AuditLog,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    SystemAccessRequest,
    User,
)
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, code
from tests.integration.job_helpers import owner_with_key

pytestmark = pytest.mark.usefixtures("assistant_master")


async def _seed(
    client: httpx.AsyncClient, db_session: AsyncSession, allowed: list | None
) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    db_session.add(
        BusinessSystem(
            key="orders",
            name="订单系统",
            allowed_bot_ids=[bot.id] if allowed == ["self"] else allowed,
        )
    )
    await followups.add(db_session, bot.id, category="promised", title="查订单")
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[1]
    )
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


async def _grants(db_session: AsyncSession, bot: Bot) -> list[tuple[str, bool]]:
    rows = await db_session.execute(
        select(BotSystemGrant.system_key, BotSystemGrant.allow_write).where(
            BotSystemGrant.bot_id == bot.id
        )
    )
    return [(key, write) for key, write in rows.all()]


async def test_owner_grants_an_open_system(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await _seed(client, db_session, None)
    r = await client.post(f"{URL}/gaps/G001/grant", json={"write": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"id": "G001", "status": "resolved"}
    assert await _grants(db_session, bot) == [("orders", False)]
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "bot.system_grants"))
    assert audit is not None and audit.actor_id == owner.id
    assert "G001" in str(audit.diff)
    r = await client.post(f"{URL}/gaps/G001/request", json={"write": False, "reason": "x"})
    assert r.status_code == 409 and code(r) == "gap_not_open"
    # 已经解决的不能再开一次。
    r = await client.post(f"{URL}/gaps/G001/grant", json={"write": False})
    assert r.status_code == 409 and code(r) == "gap_not_open"


async def test_owner_grants_write_on_an_allowlisted_system(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await _seed(client, db_session, ["self"])
    r = await client.post(f"{URL}/gaps/G001/grant", json={"write": True})
    assert r.status_code == 200, r.text
    assert await _grants(db_session, bot) == [("orders", True)]


async def test_owner_requests_and_withdraws(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await _seed(client, db_session, [])
    r = await client.post(f"{URL}/gaps/G001/grant", json={"write": False})
    assert r.status_code == 409 and code(r) == "system_not_requestable"
    assert await _grants(db_session, bot) == []
    r = await client.post(
        f"{URL}/gaps/G001/request", json={"write": True, "reason": "查客户订单状态"}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == "G001" and data["status"] == "requested" and data["request_id"]
    again = await client.post(f"{URL}/gaps/G001/request", json={"write": True, "reason": "再来"})
    assert again.status_code == 409
    board = (await client.get(f"{URL}/board")).json()["data"]
    assert board["gaps"][0]["status"] == "requested"
    assert board["gaps"][0]["request"]["status"] == "pending"
    assert board["gaps"][0]["request"]["reason"] == "查客户订单状态"
    assert board["gaps"][0]["request"]["decision_note"] is None
    r = await client.post(f"{URL}/gaps/G001/withdraw")
    assert r.status_code == 200 and r.json()["data"] == {"id": "G001", "status": "open"}
    rows = list(await db_session.scalars(select(SystemAccessRequest.status)))
    assert rows == ["withdrawn"]
    r = await client.post(f"{URL}/gaps/G001/withdraw")
    assert r.status_code == 404 and code(r) == "request_not_found"
    # 白名单仍然没有它：页面上的申请不改白名单。
    system = await db_session.get(BusinessSystem, "orders", populate_existing=True)
    assert system is not None and system.allowed_bot_ids == []


async def test_reason_limits(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await _seed(client, db_session, [])
    for reason in ["  ", "长" * 301]:
        r = await client.post(f"{URL}/gaps/G001/request", json={"write": False, "reason": reason})
        assert r.status_code == 422, reason
    r = await client.post(f"{URL}/gaps/G001/request", json={"write": False})
    assert r.status_code == 422
    assert list(await db_session.scalars(select(SystemAccessRequest.id))) == []


async def test_self_grant_on_a_requested_gap_withdraws_the_request(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # 申请之后管理员直接把它加进了白名单：所有者自己开通，在途的申请随之撤回。
    bot, _ = await _seed(client, db_session, [])
    r = await client.post(f"{URL}/gaps/G001/request", json={"write": False, "reason": "查订单"})
    assert r.status_code == 200, r.text
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None
    system.allowed_bot_ids = [bot.id]
    await db_session.commit()
    r = await client.post(f"{URL}/gaps/G001/grant", json={"write": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"id": "G001", "status": "resolved"}
    assert list(await db_session.scalars(select(SystemAccessRequest.status))) == ["withdrawn"]


async def test_unknown_gap_is_404(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await _seed(client, db_session, None)
    for path, body in [
        ("grant", {"write": False}),
        ("request", {"write": False, "reason": "x"}),
        ("withdraw", None),
    ]:
        r = await client.post(f"{URL}/gaps/G009/{path}", json=body)
        assert r.status_code == 404 and code(r) == "gap_not_found", path
