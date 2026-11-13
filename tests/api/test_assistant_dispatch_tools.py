"""个人工具 MCP 上的调度工具：主会话可用、子任务没有；只读直接派出并记审计，要改动先过审批。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from coreman.core.assistants import dispatch
from coreman.core.assistants.approvals import tower
from coreman.core.assistants.kinds import BOT_HELP_KIND, HANDOVER_KIND, SUBTASK_KIND
from coreman.core.db.models import AssistantApproval, AssistantJob, AuditLog, Task
from tests.api.test_assistant_approval_gate import listed, owner_turn
from tests.api.test_assistant_tool_groups import call, platform_turn
from tests.api.test_feishu_personal import headers
from tests.integration.approval_helpers import CFG, install_tower
from tests.integration.assistant_helpers import SECRET, identified
from tests.integration.dispatch_helpers import QUESTION, target_bot

INVALID = "invalid_tool_or_arguments"
NAMES = {"assistant_ask_bot", "assistant_bot_list"}
DISPATCH_CFG = tower.Config(
    "https://tower.example.com",
    "sender-key",
    "dispatch-key",
    "https://coreman.example.com",
    tower.DISPATCH_TARGET,
)


async def owner_session(session: Any, app: Any) -> tuple[Any, Any, Any]:
    """所有者私聊助理的一轮，助理记下了与这一轮一致的身份。"""
    bot, user, task = await owner_turn(session, app, assistant=True)
    await identified(
        session, bot, open_id="ou_human", tenant_key="tenant-test", platform_user_id="human"
    )
    return bot, user, task


async def count(session: Any, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def rounds(session: Any) -> int:
    return await count(session, Task, Task.kind == BOT_HELP_KIND)


def tower_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """两个目标系统都登记了的部署，审批系统是替身。"""
    monkeypatch.setattr(
        tower,
        "config",
        lambda _settings, target=tower.TARGET: (
            DISPATCH_CFG if target == tower.DISPATCH_TARGET else CFG
        ),
    )
    return install_tower(monkeypatch)


async def test_the_main_session_lists_and_dispatches(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tower_on(monkeypatch)
    bot, user, task = await owner_session(db_session, app)
    target = await target_bot(db_session, name="库存助手")
    assert NAMES <= set(await listed(client, app, task, user))
    auth = await headers(app, task, user)
    found = await call(client, auth, "assistant_bot_list", {})
    assert found["bots"] == [{"bot": "demo-bot", "name": "库存助手", "description": "查库存与订单"}]
    assert found["limits"]["question_chars"] == dispatch.MAX_QUESTION
    out = await call(client, auth, "assistant_ask_bot", {"bot": "库存助手", "question": QUESTION})
    assert (out["bot"], out["name"]) == ("demo-bot", "库存助手")
    assert (out["job"]["kind"], out["job"]["status"]) == ("bot_help", "queued")
    # 工具结果里没有问题原文；只读调度不经审批。
    assert SECRET not in str(out) and fake.submitted == []
    assert await rounds(db_session) == 1
    row = await db_session.scalar(select(AssistantJob))
    assert row is not None and (row.bot_id, row.target_bot_id) == (bot.id, target.id)
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == dispatch.AUDIT_ACTION)
    )
    assert audit is not None and audit.diff["unattended"] is False
    assert audit.diff["allow_write"] is False and audit.target_id == str(target.id)


async def test_a_subtask_turn_has_neither_tool(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, turn = await platform_turn(
        db_session, app, SUBTASK_KIND, session_key="oc_private#job:1"
    )
    await target_bot(db_session)
    assert not NAMES & set(await listed(client, app, turn, user))
    auth = await headers(app, turn, user)
    asked = await call(client, auth, "assistant_ask_bot", {"bot": "demo-bot", "question": "x"})
    assert asked["error"] == INVALID
    assert (await call(client, auth, "assistant_bot_list", {}))["error"] == INVALID
    assert await rounds(db_session) == 0 and await count(db_session, AssistantJob) == 0


async def test_an_unattended_turn_dispatches_read_only_without_approval(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tower_on(monkeypatch)
    _, user, turn = await platform_turn(db_session, app, HANDOVER_KIND)
    await target_bot(db_session)
    auth = await headers(app, turn, user)
    out = await call(client, auth, "assistant_ask_bot", {"bot": "demo-bot", "question": QUESTION})
    # 裁定 17：无人在场的只读调度直接派出，不提交审批单；审计里记着「无人在场」。
    assert out["job"]["status"] == "queued" and fake.submitted == []
    assert await rounds(db_session) == 1 and await count(db_session, AssistantApproval) == 0
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == dispatch.AUDIT_ACTION)
    )
    assert audit is not None and audit.diff["unattended"] is True
    assert SECRET not in str(audit.diff)


@pytest.mark.parametrize("unattended", [False, True])
async def test_allow_write_waits_for_approval(
    client: httpx.AsyncClient,
    app: Any,
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    unattended: bool,
) -> None:
    fake = tower_on(monkeypatch)
    if unattended:
        _, user, task = await platform_turn(db_session, app, HANDOVER_KIND)
    else:
        _, user, task = await owner_session(db_session, app)
    await target_bot(db_session)
    auth = await headers(app, task, user)
    arguments = {"bot": "demo-bot", "question": QUESTION, "allow_write": True}
    out = await call(client, auth, "assistant_ask_bot", arguments)
    assert out["status"] == "pending_approval" and out["bot"] == "demo-bot"
    # 未批准前不派出：没有求助的行、没有目标那一轮、没有审计。
    assert await rounds(db_session) == 0 and await count(db_session, AssistantJob) == 0
    assert await count(db_session, AuditLog, AuditLog.action == dispatch.AUDIT_ACTION) == 0
    (sent,) = fake.submitted
    assert sent["abstract"] == "调度员工 → demo-bot" and QUESTION in sent["content"]
    row = await db_session.scalar(select(AssistantApproval))
    assert row is not None and (row.tool, row.status) == ("assistant_ask_bot", "pending")


async def test_allow_write_fails_closed_without_the_target_system(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_session(db_session, app)
    await target_bot(db_session)
    auth = await headers(app, task, user)
    arguments = {"bot": "demo-bot", "question": QUESTION, "allow_write": True}
    out = await call(client, auth, "assistant_ask_bot", arguments)
    assert out["error"] == "approval_service_unavailable"
    assert await rounds(db_session) == 0 and await count(db_session, AssistantApproval) == 0


async def test_refusals_carry_a_code_and_leave_nothing_behind(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_session(db_session, app)
    await target_bot(db_session)
    await target_bot(db_session, key="quiet-bot", name="不接求助", accept=False)
    auth = await headers(app, task, user)
    long = await call(
        client,
        auth,
        "assistant_ask_bot",
        {"bot": "demo-bot", "question": "问" * (dispatch.MAX_QUESTION + 1)},
    )
    assert (long["error"], long["limit"]) == ("question_too_long", dispatch.MAX_QUESTION)
    cases = (
        ({"bot": "nobody", "question": "x"}, "bot_not_found"),
        ({"bot": "quiet-bot", "question": "x"}, "bot_not_available"),
        ({"bot": "demo-bot", "question": "   "}, "invalid_question"),
    )
    for arguments, code in cases:
        out = await call(client, auth, "assistant_ask_bot", arguments)
        assert out["error"] == code and out["hint"]
    wrong = await call(client, auth, "assistant_ask_bot", {"bot": "demo-bot"})
    assert wrong["error"] == INVALID
    assert await rounds(db_session) == 0 and await count(db_session, AssistantJob) == 0
    assert await count(db_session, AuditLog, AuditLog.action == dispatch.AUDIT_ACTION) == 0


async def test_an_ordinary_employee_has_no_dispatch_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=False)
    await target_bot(db_session)
    assert not NAMES & set(await listed(client, app, task, user))
    auth = await headers(app, task, user)
    out = await call(client, auth, "assistant_ask_bot", {"bot": "demo-bot", "question": "x"})
    assert "error" in out and await rounds(db_session) == 0


async def test_an_unattended_turn_cannot_reach_someone_elses_private_runtime(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tower_on(monkeypatch)
    _, user, turn = await platform_turn(db_session, app, HANDOVER_KIND)
    # 同事自己建的员工，跑在他的私有运行时上：他在自己的节点上读得到问题。
    await target_bot(db_session, key="their-bot", name="同事的员工", private_to="creator")
    await target_bot(db_session, key="open-bot", name="公开的员工")
    await target_bot(db_session, key="my-bot", name="我的员工", private_to=user.id)
    auth = await headers(app, turn, user)
    listed_bots = (await call(client, auth, "assistant_bot_list", {}))["bots"]
    assert sorted(row["bot"] for row in listed_bots) == ["my-bot", "open-bot"]
    for extra in ({}, {"allow_write": True}):
        out = await call(
            client, auth, "assistant_ask_bot", {"bot": "their-bot", "question": QUESTION, **extra}
        )
        assert out["error"] == "owner_turn_required" and out["hint"]
    # 提交时就拒：没有派出，也没有审批单。
    assert await rounds(db_session) == 0 and await count(db_session, AssistantApproval) == 0
    assert fake.submitted == []
    # 公开运行时、所有者自己的私有运行时照常。
    for key in ("open-bot", "my-bot"):
        out = await call(client, auth, "assistant_ask_bot", {"bot": key, "question": QUESTION})
        assert out["job"]["status"] == "queued"
    assert await rounds(db_session) == 2


async def test_the_owner_in_person_may_ask_an_employee_on_a_private_runtime(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_session(db_session, app)
    await target_bot(db_session, key="their-bot", name="同事的员工", private_to="creator")
    auth = await headers(app, task, user)
    listed_bots = (await call(client, auth, "assistant_bot_list", {}))["bots"]
    assert [row["bot"] for row in listed_bots] == ["their-bot"]
    out = await call(client, auth, "assistant_ask_bot", {"bot": "their-bot", "question": QUESTION})
    assert out["job"]["status"] == "queued" and await rounds(db_session) == 1
