"""缺口工具：经注册表分派；只有助理能用；不开的缺口给提示；错误只给代码与提示。"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_requests, followups, gap_tools, gaps, request_cards
from coreman.core.assistants import tools as registry
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BusinessSystem,
    OutboxItem,
    SystemAccessRequest,
    User,
)
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import assistant_turn
from tests.integration.worker_helpers import MASTER

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
        "need_write": False,
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


CIPHER = Cipher(MASTER)


async def _requestable(db_session: AsyncSession) -> tuple[Bot, User, Scope]:
    bot, owner, scope = await assistant_turn(db_session)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=[]))
    await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="查订单 私密",
        followup_seqs=[],
    )
    await db_session.commit()
    return bot, owner, scope


async def _stage(
    db_session: AsyncSession, scope: Scope, reason: str = "查客户订单状态"
) -> OutboxItem:
    out = await gap_tools.dispatch(
        db_session, CIPHER, scope, "gap_request", {"id": "G001", "reason": reason}
    )
    await db_session.commit()
    assert out["status"] == "confirmation_sent", out
    sent = await db_session.scalar(
        select(OutboxItem)
        .where(OutboxItem.bot_id == scope.bot.id, OutboxItem.kind == "send")
        .order_by(OutboxItem.created_at.desc())
        .limit(1)
    )
    assert sent is not None
    return sent


def _value(sent: OutboxItem, label: str) -> dict[str, Any]:
    for element in sent.payload["card"]["body"]["elements"]:
        if element.get("tag") == "button" and element["text"]["content"] == label:
            return dict(element["behaviors"][0]["value"])
    raise AssertionError(label)


async def _click(
    db_session: AsyncSession, bot: Bot, value: dict[str, Any], who: str = "zhangsan"
) -> str:
    inbound: Any = SimpleNamespace(
        id=uuid.uuid4(),
        chat_id="oc_assistant",
        sender_platform_user_id=who,
        reply_context={"message_id": "om_card"},
    )
    result = await request_cards.decide(db_session, CIPHER, bot, inbound, value)
    await db_session.commit()
    return result


async def _requests(db_session: AsyncSession) -> list[SystemAccessRequest]:
    return list(await db_session.scalars(select(SystemAccessRequest)))


async def test_gap_request_needs_an_attended_turn(db_session: AsyncSession) -> None:
    _, _, scope = await _requestable(db_session)
    # 只换任务类型：工具只读 scope.unattended 判断在不在场。
    unattended = replace(scope, task=SimpleNamespace(kind="assistant_handover"))
    out = await call(db_session, unattended, "gap_request", id="G001", reason="查客户订单状态")
    assert out["error"] == "owner_turn_required" and out["hint"]
    platform = replace(scope, platform=True)
    out = await call(db_session, platform, "gap_request", id="G001", reason="查客户订单状态")
    assert out["error"] == "owner_turn_required"
    assert await _requests(db_session) == []
    assert await db_session.scalar(select(OutboxItem.id)) is None


async def test_gap_request_sends_a_card_and_only_the_owners_click_submits(
    db_session: AsyncSession,
) -> None:
    bot, owner, scope = await _requestable(db_session)
    sent = await _stage(db_session, scope)
    assert sent.target == {"chat_id": "oc_assistant"}
    text = str(sent.payload["card"])
    assert "订单系统" in text and "只读" in text and "查客户订单状态" in text
    assert "私密" not in text
    # 发卡片不建单，缺口仍待开通。
    assert await _requests(db_session) == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"
    submit = _value(sent, "提交申请")
    # 别人点、改过的值、过期的令牌：都不建单。
    assert await _click(db_session, bot, submit, who="lisi") == "ignored"
    assert await _click(db_session, bot, {**submit, "reason": "改过的理由"}) == "ignored"
    assert await _click(db_session, bot, {**submit, "write": True}) == "ignored"
    assert await _requests(db_session) == []
    assert await _click(db_session, bot, submit) == "gap_request_submitted"
    [row] = await _requests(db_session)
    assert (row.status, row.reason, row.write, row.requested_by) == (
        "pending",
        "查客户订单状态",
        False,
        owner.id,
    )
    # 同一张卡再点一次（重放）：不再建单。
    assert await _click(db_session, bot, submit) == "gap_request_stale"
    await access_requests.withdraw(db_session, bot.id, 1)
    await db_session.commit()
    assert await _click(db_session, bot, submit) == "gap_request_stale"
    assert [r.status for r in await _requests(db_session)] == ["withdrawn"]


async def test_gap_request_card_expires(db_session: AsyncSession) -> None:
    bot, _, scope = await _requestable(db_session)
    with patch("coreman.core.assistants.request_cards.time.time", return_value=time.time() - 7200):
        sent = await _stage(db_session, scope)
    assert await _click(db_session, bot, _value(sent, "提交申请")) == "gap_request_expired"
    assert await _requests(db_session) == []


async def test_dropping_the_card_voids_it(db_session: AsyncSession) -> None:
    bot, _, scope = await _requestable(db_session)
    sent = await _stage(db_session, scope)
    assert await _click(db_session, bot, _value(sent, "不提交")) == "gap_request_dropped"
    assert await _click(db_session, bot, _value(sent, "提交申请")) == "gap_request_stale"
    assert await _requests(db_session) == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"


async def test_gap_request_asks_for_write_when_read_was_already_there(
    db_session: AsyncSession,
) -> None:
    bot, owner, scope = await _requestable(db_session)
    gap = await gaps.get(db_session, bot.id, 1)
    gap.seen_rank = 1
    await db_session.commit()
    sent = await _stage(db_session, scope)
    assert "读写" in str(sent.payload["card"])
    assert await _click(db_session, bot, _value(sent, "提交申请")) == "gap_request_submitted"
    [row] = await _requests(db_session)
    assert row.write is True


async def test_gap_request_errors(db_session: AsyncSession) -> None:
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
    assert await db_session.scalar(select(OutboxItem.id)) is None
