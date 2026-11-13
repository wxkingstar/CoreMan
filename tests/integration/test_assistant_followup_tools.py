"""待跟进工具：经分派读写，日期按所有者时区，错误只给代码。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followup_tools, followups, keys
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants import tools as registry
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import SECRET, raw, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
# 待跟进工具不用平台主密钥：内容由会话按所有者的数据密钥加解密。
NO_CIPHER: Any = None
STORED = "SELECT title_enc || notes_enc FROM assistant_followups WHERE bot_id = :bot"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # UTC 11 月 13 日 02:00：上海是 13 日上午，纽约还是 12 日晚上。
    monkeypatch.setattr(followup_tools, "utcnow", lambda: datetime(2026, 11, 13, 2, 0, tzinfo=UTC))


async def call(
    session: AsyncSession, scope: Scope, name: str, /, **arguments: Any
) -> dict[str, Any]:
    """前三个参数只按位置传：工具自己也有一个叫 `scope` 的入参。"""
    out = await followup_tools.dispatch(session, NO_CIPHER, scope, name, arguments)
    await session.commit()
    return out


async def test_add_list_update_snooze_close(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    added = await call(
        db_session,
        scope,
        "followup_add",
        category="promised",
        title=f"周四前给李四报价 {SECRET}",
        notes="他周一问的",
        due_on="2026-11-13",
        origin_message_id="om_2",
    )
    assert added["today"] == "2026-11-13"
    assert added["followup"]["id"] == "F001" and added["followup"]["status"] == "open"
    assert added["followup"]["notes"] == "他周一问的"
    stored = await raw(db_session, STORED, bot=bot.id)
    assert SECRET not in str(stored) and "李四" not in str(stored)
    await call(db_session, scope, "followup_add", category="remind", title="续费")

    # 清单里只说有没有备注；按编号取一条才给全文。scope 不给时是 today。
    listed = await call(db_session, scope, "followup_list")
    assert [item["id"] for item in listed["items"]] == ["F001"]
    assert listed["items"][0]["has_notes"] is True and "notes" not in listed["items"][0]
    assert listed["truncated"] is False
    one = await call(db_session, scope, "followup_list", scope="f1")
    assert one["followup"]["notes"] == "他周一问的"
    opened = await call(db_session, scope, "followup_list", scope="open", limit=1)
    assert [item["id"] for item in opened["items"]] == ["F001"] and opened["truncated"] is True

    updated = await call(
        db_session,
        scope,
        "followup_update",
        id="F001",
        category="waiting",
        review_on="2026-11-14",
        clear=["notes", "due_on"],
    )
    assert updated["followup"]["category"] == "waiting"
    assert updated["followup"]["notes"] is None and updated["followup"]["due_on"] is None
    assert updated["followup"]["review_on"] == "2026-11-14"

    snoozed = await call(db_session, scope, "followup_snooze", id="F002", until="2026-11-20")
    assert (snoozed["followup"]["status"], snoozed["followup"]["review_on"]) == (
        "snoozed",
        "2026-11-20",
    )
    closed = await call(
        db_session, scope, "followup_close", id="F001", outcome="dropped", notes="对方说不用了"
    )
    assert closed["followup"]["status"] == "dropped"
    assert closed["followup"]["notes"] == "对方说不用了"
    everything = await call(db_session, scope, "followup_list", scope="all")
    assert [(item["id"], item["status"]) for item in everything["items"]] == [
        ("F002", "snoozed"),
        ("F001", "dropped"),
    ]


async def test_dates_follow_the_owners_timezone(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    row = await assistant_settings.get(db_session, bot.id)
    row.timezone = "America/New_York"
    await db_session.flush()
    added = await call(db_session, scope, "followup_add", category="remind", title="交报告")
    assert added["today"] == "2026-11-12"
    # 纽约的今天是 12 日：13 日是明天，可以推过去。
    out = await call(db_session, scope, "followup_snooze", id="F001", until="2026-11-13")
    assert out["followup"]["status"] == "snoozed"
    # 12 日还没到回看日。
    assert (await call(db_session, scope, "followup_list"))["items"] == []
    row.timezone = "Asia/Shanghai"
    await db_session.flush()
    # 上海已经是 13 日：它到期了，重新出现；再推到 13 日就不是「之后」了。
    listed = await call(db_session, scope, "followup_list")
    assert [(item["id"], item["status"]) for item in listed["items"]] == [("F001", "open")]
    refused = await call(db_session, scope, "followup_snooze", id="F001", until="2026-11-13")
    assert refused["error"] == "snooze_not_future" and refused["today"] == "2026-11-13"


async def test_errors_are_codes_without_content(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, scope = await assistant_turn(db_session)
    assert await call(db_session, scope, "notes_read") == {"error": "invalid_tool_or_arguments"}
    for name, arguments in (
        ("followup_add", {"category": "todo", "title": "x"}),
        ("followup_add", {"category": "remind", "title": "x", "due_on": "2026-02-31"}),
        ("followup_add", {"category": "remind", "title": "x", "owner": "someone"}),
        ("followup_add", {"category": "remind", "title": "长" * (followups.MAX_TITLE + 1)}),
        ("followup_update", {"id": "F001", "clear": ["title"]}),
        ("followup_snooze", {"id": "one", "until": "2026-11-20"}),
        ("followup_list", {"limit": 0}),
    ):
        out = await call(db_session, scope, name, **arguments)
        assert out["error"] == "invalid_tool_or_arguments", (name, arguments)
        # 只有字段名和规则，不回显取值。
        assert out["invalid"] and "someone" not in str(out) and "长长" not in str(out)
    missing = await call(db_session, scope, "followup_update", id="F099", title="改")
    assert missing["error"] == "followup_not_found" and "hint" in missing
    await call(db_session, scope, "followup_add", category="remind", title="一件事")
    assert (await call(db_session, scope, "followup_update", id="F001"))["error"] == (
        "nothing_to_update"
    )
    assert (await call(db_session, scope, "followup_list", scope="soon"))["error"] == (
        "invalid_scope"
    )
    await call(db_session, scope, "followup_close", id="F001")
    assert (await call(db_session, scope, "followup_update", id="F001", title="改"))["error"] == (
        "followup_closed"
    )
    monkeypatch.setattr(followups, "MAX_OPEN", 1)
    await call(db_session, scope, "followup_add", category="remind", title="第二件")
    full = await call(db_session, scope, "followup_add", category="remind", title="第三件")
    assert (full["error"], full["limit"]) == ("too_many_followups", 1)


async def test_the_registry_routes_them_in_the_knowledge_group(db_session: AsyncSession) -> None:
    """经助理工具注册表分派：knowledge 组放行，子任务那样只有 knowledge 组的轮次也能用。"""
    bot, _, scope = await assistant_turn(db_session)
    ctx = registry.Context(db_session, NO_CIPHER, scope)
    added = await registry.dispatch(
        ctx, "followup_add", {"category": "remind", "title": "续费"}, groups=("knowledge",)
    )
    assert added["followup"]["id"] == "F001"
    refused = await registry.dispatch(ctx, "followup_list", {}, groups=("approval", "jobs"))
    assert refused == {"error": "invalid_tool_or_arguments"}
    assert {registry.group_of(name) for name in followup_tools.OWN} == {"knowledge"}


async def test_only_assistants_have_these_tools(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    standard = await seed_standard(db_session)
    out = await call(db_session, replace(scope, bot=standard), "followup_list")
    assert out == {"error": "invalid_tool_or_arguments"}


async def test_another_assistants_scope_sees_nothing(db_session: AsyncSession) -> None:
    _, _, mine = await assistant_turn(db_session)
    await call(db_session, mine, "followup_add", category="decide", title=f"我的事 {SECRET}")
    _, _, theirs = await assistant_turn(db_session, who="lisi")
    assert (await call(db_session, theirs, "followup_list", scope="all"))["items"] == []
    assert (await call(db_session, theirs, "followup_list", scope="F001"))["error"] == (
        "followup_not_found"
    )
    assert (await call(db_session, theirs, "followup_close", id="F001"))["error"] == (
        "followup_not_found"
    )


async def test_a_destroyed_key_is_reported_not_swallowed(db_session: AsyncSession) -> None:
    _, owner, scope = await assistant_turn(db_session)
    await call(db_session, scope, "followup_add", category="remind", title="续费")
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    out = await call(db_session, scope, "followup_add", category="remind", title="新的")
    assert out["error"] == "content_locked" and "hint" in out
    listed = await call(db_session, scope, "followup_list", scope="open")
    assert [(item["id"], item["title"], item["locked"]) for item in listed["items"]] == [
        ("F001", "", True)
    ]
