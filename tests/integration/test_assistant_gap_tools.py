"""缺口工具：经注册表分派；只有助理能用；不开的缺口给提示；错误只给代码与提示。"""

from __future__ import annotations

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
