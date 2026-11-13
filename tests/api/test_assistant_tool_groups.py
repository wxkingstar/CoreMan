"""个人工具 MCP 上助理自有工具的分组：按任务类型列出与放行，knowledge 组不受授权换代影响。"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from coreman.core.assistants import platform_turns, tools
from coreman.core.assistants.kinds import HANDOVER_KIND, SUBTASK_KIND
from coreman.core.bus import tasks
from coreman.core.db.models import FeishuPersonalGrant
from tests.api.test_assistant_approval_gate import listed, owner_turn
from tests.api.test_feishu_personal import URL, headers, rpc, value
from tests.integration.assistant_helpers import identified, isolate_tool_registry
from tests.integration.test_cron_handler import claim

INVALID = "invalid_tool_or_arguments"


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], uuid.UUID]]:
    """knowledge 组里登记一个演示工具，记下每次调用（工具名、入参、所有者）。"""
    isolate_tool_registry(monkeypatch)
    calls: list[tuple[str, dict[str, Any], uuid.UUID]] = []

    async def run(ctx: tools.Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments, ctx.scope.user_id))
        return {"ok": True}

    tools.register(
        "knowledge",
        lambda: [
            {
                "name": "notes_demo",
                "description": "demo",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        run,
    )
    return calls


async def call(
    client: httpx.AsyncClient, auth: dict[str, str], name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = value(await client.post(URL, headers=auth, json=rpc(name, arguments)))
    return out


async def platform_turn(session: Any, app: Any, kind: str, **kw: Any) -> tuple[Any, Any, Any]:
    """所有者的助理里一轮平台来源的任务（已认领）。此前那一轮私聊已经结束。"""
    bot, user, task = await owner_turn(session, app, assistant=True)
    # 与 owner_turn 造出的通讯录身份、授权一致。
    await identified(
        session, bot, open_id="ou_human", tenant_key="tenant-test", platform_user_id="human"
    )
    await tasks.finish(session, task.id, status="succeeded")
    queued = await platform_turns.enqueue(session, bot, kind, {}, dedupe_key=f"{kind}:1", **kw)
    await session.commit()
    assert queued is not None
    turn = await claim(session)
    assert turn.id == queued.id
    return bot, user, turn


async def test_a_private_turn_lists_and_runs_knowledge_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=True)
    listing = await listed(client, app, task, user)
    assert {"notes_demo", "approval_list", "feishu_chat_history", "feishu_authorize"} <= set(
        listing
    )
    auth = await headers(app, task, user)
    assert await call(client, auth, "notes_demo", {"q": "报价"}) == {"ok": True}
    assert notes == [("notes_demo", {"q": "报价"}, user.id)]


async def test_knowledge_tools_survive_a_changed_authorization(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    bot, user, task = await owner_turn(db_session, app, assistant=True)
    auth = await headers(app, task, user)
    # 这一轮开始之后授权换了代（撤销、重选或重新绑定都会这样）。
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    grant.context_epoch = uuid.uuid4()
    await db_session.commit()
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert await call(client, auth, "approval_list", {}) == {"error": "authorization_changed"}
    assert await call(client, auth, "feishu_chat_history", {"chat_id": "oc_li"}) == {
        "error": "authorization_changed"
    }
    assert len(notes) == 1


async def test_a_standard_bot_never_reaches_assistant_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=False)
    assert "notes_demo" not in await listed(client, app, task, user)
    auth = await headers(app, task, user)
    assert (await call(client, auth, "notes_demo", {}))["error"] == INVALID
    assert notes == []


async def test_a_platform_turn_gets_the_owners_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, turn = await platform_turn(db_session, app, HANDOVER_KIND)
    assert turn.inbound_event_id is None
    listing = await listed(client, app, turn, user)
    # 和私聊一轮一样：读写个人工具（写类过审批）、授权工具、审批组与 knowledge 组。
    assert {"notes_demo", "approval_list", "feishu_chat_history", "feishu_authorize"} <= set(
        listing
    )
    assert "approval" in listing["feishu_reply_message"]["inputSchema"]["properties"]
    auth = await headers(app, turn, user)
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert notes[-1][2] == user.id
    # 别人拿着这一轮的任务号签不出能用的令牌。
    forged = await headers(app, turn, type("Someone", (), {"id": uuid.uuid4()})())
    response = await client.post(URL, headers=forged, json=rpc("notes_demo"))
    assert response.status_code == 403


async def test_a_subtask_is_read_only_plus_knowledge(
    client: httpx.AsyncClient, app: Any, db_session: Any, notes: list[Any]
) -> None:
    _, user, turn = await platform_turn(
        db_session, app, SUBTASK_KIND, session_key="oc_private#job:1"
    )
    listing = await listed(client, app, turn, user)
    assert "notes_demo" in listing and "feishu_chat_history" in listing
    assert "feishu_reply_message" not in listing
    assert not [name for name in listing if name.startswith("approval_")]
    assert "assistant_mark_processing" not in listing
    auth = await headers(app, turn, user)
    assert await call(client, auth, "notes_demo", {}) == {"ok": True}
    assert (await call(client, auth, "approval_list", {}))["error"] == INVALID
    assert (await call(client, auth, "feishu_reply_message", {}))["error"] == INVALID
