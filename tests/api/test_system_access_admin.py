"""平台管理员处理申请：批准后白名单与授权都有了、缺口解决、被卡住的事交回；拒绝带理由；别人碰不到。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_requests, followups, gaps
from coreman.core.db.models import (
    AuditLog,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    SystemAccessRequest,
)
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import as_bot_token
from tests.integration.job_helpers import owner_with_key

pytestmark = pytest.mark.usefixtures("assistant_master")
PATH = "/api/admin/system-access-requests"


async def _requested(db_session: AsyncSession):  # type: ignore[no-untyped-def]
    bot, owner = await owner_with_key(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=[]))
    await followups.add(db_session, bot.id, category="promised", title="查订单")
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[1]
    )
    row = await access_requests.open_request(
        db_session, bot, 1, write=True, reason="查客户订单状态", actor=owner
    )
    await db_session.commit()
    # 之后会 expire_all：先把编号取成普通值。
    return bot.id, row.id


async def test_manager_approves(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot_id, row_id = await _requested(db_session)
    await login_as(client, db_session, role="platform_admin")
    listed = (await client.get(PATH)).json()["data"]
    assert [i["id"] for i in listed["items"]] == [str(row_id)]
    assert listed["items"][0]["reason"] == "查客户订单状态"
    assert listed["items"][0]["system_name"] == "订单系统" and listed["total"] == 1
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "approved"
    db_session.expire_all()
    grant = await db_session.scalar(select(BotSystemGrant).where(BotSystemGrant.bot_id == bot_id))
    assert grant is not None and grant.system_key == "orders" and grant.allow_write
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == [bot_id]
    assert (await gaps.get(db_session, bot_id, 1)).status == "resolved"
    logs = {row.action: row for row in await db_session.scalars(select(AuditLog))}
    assert {"system_access.approve", "bot.system_grants", "system.update"} <= set(logs)
    assert logs["system_access.approve"].diff == {"system": "orders", "write": True}
    assert logs["system.update"].diff == {"granted_bot_ids": [[], [str(bot_id)]]}
    # 处理过的不在「待处理」里，「全部」里能看到。
    assert (await client.get(PATH)).json()["data"]["items"] == []
    everything = (await client.get(PATH, params={"status": "all"})).json()["data"]["items"]
    assert [i["status"] for i in everything] == ["approved"]
    # 再点一次：已经处理过了。
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 409 and r.json()["errors"][0]["type"] == "request_decided"


async def test_manager_rejects_with_a_note(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, row_id = await _requested(db_session)
    await login_as(client, db_session, role="ai_committee")
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "reject", "note": ""})
    assert r.status_code == 422
    r = await client.post(
        f"{PATH}/{row_id}/decide", json={"decision": "reject", "note": "先找业务负责人确认"}
    )
    assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "open"
    decided = await db_session.get(SystemAccessRequest, row_id)
    assert decided is not None and decided.decision_note == "先找业务负责人确认"
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert "system_access.reject" in actions and "bot.system_grants" not in actions


async def test_stale_request_is_withdrawn_and_reported(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, row_id = await _requested(db_session)
    bot = await db_session.get(Bot, bot_id)
    assert bot is not None
    bot.enabled = False
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 409 and r.json()["errors"][0]["type"] == "request_stale"
    db_session.expire_all()
    # 撤回已经提交：不随错误一起回滚；白名单与授权都没动。
    stale = await db_session.get(SystemAccessRequest, row_id)
    assert stale is not None and stale.status == "withdrawn" and stale.decision_note
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == []
    assert await db_session.scalar(select(BotSystemGrant.bot_id)) is None


async def test_unknown_request(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(
        f"{PATH}/00000000-0000-0000-0000-000000000000/decide",
        json={"decision": "approve", "note": ""},
    )
    assert r.status_code == 404 and r.json()["errors"][0]["type"] == "request_not_found"


@pytest.mark.parametrize("role", ["team_lead", "member"])
async def test_others_cannot_see_or_decide(
    client: httpx.AsyncClient, db_session: AsyncSession, role: str
) -> None:
    _, row_id = await _requested(db_session)
    await login_as(client, db_session, role=role)
    assert (await client.get(PATH)).status_code == 403
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 403


async def test_approving_keeps_existing_grants(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, row_id = await _requested(db_session)
    db_session.add(BusinessSystem(key="aaa", name="甲", allowed_bot_ids=[bot_id]))
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot_id, system_key="aaa", allow_write=True))
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    grants = {
        g.system_key: g.allow_write
        for g in await db_session.scalars(
            select(BotSystemGrant).where(BotSystemGrant.bot_id == bot_id)
        )
    }
    assert grants == {"aaa": True, "orders": True}


async def test_an_existing_grant_no_longer_open_is_named(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, row_id = await _requested(db_session)
    db_session.add(BusinessSystem(key="aaa", name="甲", allowed_bot_ids=[bot_id], enabled=False))
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot_id, system_key="aaa"))
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": "approve", "note": ""})
    assert r.status_code == 409 and r.json()["errors"][0]["type"] == "existing_grant_blocked"
    assert "aaa" in r.json()["message"]
    db_session.expire_all()
    # 整个批准回滚：申请仍在途，白名单没动。
    pending = await db_session.get(SystemAccessRequest, row_id)
    assert pending is not None and pending.status == "pending"
    system = await db_session.get(BusinessSystem, "orders")
    assert system is not None and system.allowed_bot_ids == []


async def test_bot_tokens_cannot_decide(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批准会放宽机器人能用的系统：只认管理员本人的交互登录，机器人令牌一律拒绝。"""
    _, row_id = await _requested(db_session)
    manager = await login_as(client, db_session, role="platform_admin")
    as_bot_token(client, monkeypatch, manager)
    assert (await client.get(PATH)).status_code == 200
    for decision, note in (("approve", ""), ("reject", "不行")):
        r = await client.post(f"{PATH}/{row_id}/decide", json={"decision": decision, "note": note})
        assert r.status_code == 403, r.text
    db_session.expire_all()
    pending = await db_session.get(SystemAccessRequest, row_id)
    assert pending is not None and pending.status == "pending"
