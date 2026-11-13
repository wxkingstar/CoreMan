"""设置的两个工具：只在所有者发起的轮次里可用；校验错误原样给模型；登记在 knowledge 组。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings, settings_tools
from coreman.core.assistants import tools as registry
from coreman.core.feishu_personal.policy import Scope
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
NAMES = {"assistant_settings_get", "assistant_settings_update"}


async def call(session: AsyncSession, scope: Scope, name: str, **arguments: Any) -> dict[str, Any]:
    out = await settings_tools.dispatch(session, None, scope, name, arguments)  # type: ignore[arg-type]
    await session.commit()
    return out


def test_both_tools_are_registered_in_the_knowledge_group() -> None:
    assert {item["name"] for item in settings_tools.DEFINITIONS} == NAMES
    assert NAMES <= {item["name"] for item in registry.definitions([], groups=("knowledge",))}
    assert {registry.group_of(name) for name in NAMES} == {"knowledge"}
    update = {item["name"]: item for item in settings_tools.DEFINITIONS}[
        "assistant_settings_update"
    ]
    assert set(update["inputSchema"]["properties"]) == {
        "timezone",
        "work_start",
        "work_end",
        "workdays",
        "focus_chats",
        "ignored_chats",
    }
    assert "required" not in update["inputSchema"] or update["inputSchema"]["required"] == []


async def test_the_owner_reads_and_changes_settings(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    bot_id = bot.id  # expire_all 之后行上的属性已过期，先取出来
    shown = await call(db_session, scope, "assistant_settings_get")
    assert shown["settings"]["work_start"] == "09:30" and shown["settings"]["focus_chats"] == []
    saved = await call(
        db_session,
        scope,
        "assistant_settings_update",
        work_start="10:00",
        work_end="20:00",
        workdays=[1, 2, 3, 4, 5, 6],
        focus_chats=["oc_team"],
    )
    assert saved["status"] == "saved"
    assert saved["settings"]["workdays"] == [1, 2, 3, 4, 5, 6]
    assert saved["settings"]["focus_chats"] == ["oc_team"]
    db_session.expire_all()
    row = await settings.get(db_session, bot_id)
    assert (row.work_start, row.work_end) == ("10:00", "20:00")
    assert settings.view(row, utcnow())["focus_chats"] == ["oc_team"]


async def test_validation_errors_reach_the_model_as_codes(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    update = "assistant_settings_update"
    assert await call(db_session, scope, update) == {"error": "nothing_to_update"}
    assert await call(db_session, scope, update, timezone="Mars/Olympus") == {
        "error": "invalid_timezone"
    }
    assert await call(db_session, scope, update, work_start="20:00") == {
        "error": "invalid_work_hours"
    }
    too_many = await call(db_session, scope, update, focus_chats=[f"oc_{n}" for n in range(6)])
    assert too_many == {"error": "too_many_chats", "limit": settings.MAX_FOCUS}
    # 类型不对由入参模型挡住。
    wrong = await call(db_session, scope, update, workdays="1,2,3")
    assert wrong["error"] == "invalid_tool_or_arguments" and wrong["invalid"]
    unknown = await call(db_session, scope, update, daily_turn_budget=9999)
    assert unknown["error"] == "invalid_tool_or_arguments"


async def test_both_tools_are_refused_when_nobody_is_present(db_session: AsyncSession) -> None:
    bot, _, owner = await assistant_turn(db_session)
    bot_id = bot.id
    unattended = replace(owner, event=None, platform=True)
    assert unattended.unattended
    for name, arguments in (
        ("assistant_settings_get", {}),
        ("assistant_settings_update", {"ignored_chats": ["oc_boss"]}),
        ("assistant_settings_update", {"work_start": "00:00", "work_end": "23:59"}),
    ):
        out = await call(db_session, unattended, name, **arguments)
        assert out == {"error": "owner_turn_required", "hint": settings_tools.OWNER_HINT}
    db_session.expire_all()
    shown = settings.view(await settings.get(db_session, bot_id), utcnow())
    assert shown["ignored_chats"] == [] and shown["work_start"] == "09:30"


async def test_an_ordinary_bot_has_no_settings_tools(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    standard = replace(scope, bot=await seed_standard(db_session))
    out = await call(db_session, standard, "assistant_settings_get")
    assert out == {"error": "invalid_tool_or_arguments"}
