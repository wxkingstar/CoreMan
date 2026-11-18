"""缺口工具：经注册表分派；只有助理能用；不开的缺口给提示；错误只给代码与提示。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, gap_tools, gaps
from coreman.core.assistants import tools as registry
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import assistant_turn

pytestmark = pytest.mark.usefixtures("assistant_master")
NO_CIPHER: Any = None


async def call(
    session: AsyncSession, scope: Scope, name: str, /, **arguments: Any
) -> dict[str, Any]:
    out = await gap_tools.dispatch(session, NO_CIPHER, scope, name, arguments)
    await session.commit()
    return out


def test_tools_are_registered_in_knowledge() -> None:
    assert registry.group_of("gap_report") == "knowledge"
    assert registry.group_of("gap_list") == "knowledge"
    assert registry.group_of("gap_request") == "knowledge"


async def test_report_list_and_declined(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    await followups.add(db_session, bot.id, category="promised", title="查退货")
    await db_session.commit()
    out = await call(
        db_session,
        scope,
        "gap_report",
        kind="feishu_scope",
        key="im:message",
        need="读群消息",
        followups=["F001"],
    )
    assert out["outcome"] == "created"
    assert out["gap"] == {
        "id": "G001",
        "kind": "feishu_scope",
        "key": "im:message",
        "need": "读群消息",
        "status": "open",
        "fix": "authorize",
        "hint": gaps.hint(await gaps.get(db_session, bot.id, 1), "authorize"),
        "followups": ["F001"],
    }
    listed = await call(db_session, scope, "gap_list")
    assert [g["id"] for g in listed["gaps"]] == ["G001"]
    await gaps.decline(db_session, bot.id, 1)
    await db_session.commit()
    again = await call(
        db_session, scope, "gap_report", kind="feishu_scope", key="im:message", need="还要"
    )
    assert again["outcome"] == "declined" and "不要再为它登记" in again["hint"]
    assert (await call(db_session, scope, "gap_list"))["gaps"] == []
    assert [
        g["id"] for g in (await call(db_session, scope, "gap_list", status="declined"))["gaps"]
    ] == ["G001"]
    assert [g["id"] for g in (await call(db_session, scope, "gap_list", status="all"))["gaps"]] == [
        "G001"
    ]


async def test_errors_carry_hints(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    out = await call(
        db_session, scope, "gap_report", kind="credential", key="GIT_TOKEN", need="x", resolved=True
    )
    assert out["error"] == "resolve_not_allowed" and "node_tool" in out["hint"]
    out = await call(
        db_session,
        scope,
        "gap_report",
        kind="other",
        key="repo-access",
        need="x",
        followups=["F009"],
    )
    assert out["error"] == "followup_not_found"
    out = await call(db_session, scope, "gap_report", kind="repo", key="x", need="x")
    assert out["error"] == "invalid_tool_or_arguments"


async def test_report_records_the_current_rank_server_side(db_session: AsyncSession) -> None:
    """工具不收级别参数：平台按这位助理现在的授权记下报缺口时的级别。"""
    from coreman.core.db.models import BotSystemGrant, BusinessSystem

    bot, _, scope = await assistant_turn(db_session)
    db_session.add(
        BusinessSystem(
            key="orders",
            name="订单系统",
            token_delivery="proxy",
            openapi_url="https://orders.example.com/openapi.json",
        )
    )
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="orders"))
    await db_session.commit()
    out = await call(
        db_session, scope, "gap_report", kind="business_system", key="orders", need="改订单"
    )
    assert out["outcome"] == "created"
    assert (await gaps.get(db_session, bot.id, 1)).seen_rank == 1
    rejected = await call(
        db_session,
        scope,
        "gap_report",
        kind="business_system",
        key="crm",
        need="x",
        seen_rank=2,
    )
    assert rejected["error"] == "invalid_tool_or_arguments"


async def test_gap_request_needs_an_attended_turn(db_session: AsyncSession) -> None:
    from sqlalchemy import select

    from coreman.core.db.models import BusinessSystem, SystemAccessRequest

    bot, owner, scope = await assistant_turn(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=[]))
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[]
    )
    await db_session.commit()
    # 只换任务类型：工具只读 scope.unattended 判断在不在场。
    unattended = replace(scope, task=SimpleNamespace(kind="assistant_handover"))
    out = await call(db_session, unattended, "gap_request", id="G001", reason="查客户订单状态")
    assert out["error"] == "owner_turn_required" and out["hint"]
    assert list(await db_session.scalars(select(SystemAccessRequest.id))) == []
    platform = replace(scope, platform=True)
    out = await call(db_session, platform, "gap_request", id="G001", reason="查客户订单状态")
    assert out["error"] == "owner_turn_required"
    out = await call(db_session, scope, "gap_request", id="G001", reason="查客户订单状态")
    assert out["status"] == "requested" and out["request_id"]
    row = await db_session.scalar(select(SystemAccessRequest))
    assert row is not None and row.requested_by == owner.id and row.reason == "查客户订单状态"
    assert row.write is False
    out = await call(db_session, scope, "gap_request", id="G001", reason="再来")
    assert out["error"] == "gap_not_open" and out["hint"]


async def test_gap_request_errors(db_session: AsyncSession) -> None:
    from coreman.core.db.models import BusinessSystem

    bot, _, scope = await assistant_turn(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=None))
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[]
    )
    await db_session.commit()
    out = await call(db_session, scope, "gap_request", id="G001", reason="查订单")
    assert out["error"] == "owner_can_grant" and "开通" in out["hint"]
    out = await call(db_session, scope, "gap_request", id="G009", reason="查订单")
    assert out["error"] == "gap_not_found"
    out = await call(db_session, scope, "gap_request", id="G001", reason="长" * 301)
    assert out["error"] == "invalid_tool_or_arguments"
