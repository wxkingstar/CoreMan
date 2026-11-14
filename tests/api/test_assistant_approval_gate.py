"""个人工具 MCP 上的审批闸门：助理的写类工具不执行，读类照常；普通机器人完全不变。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, select

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.approvals import catalog, tower
from coreman.core.db.models import AssistantApproval, InteractionState
from coreman.core.feishu_personal import endpoints
from tests.api.test_feishu_personal import URL, grant, headers, rpc, setup, value
from tests.integration.approval_helpers import install_tower, message, reply
from tests.integration.assistant_helpers import into_assistant

FEISHU = "https://open.feishu.cn/open-apis"
EVERY_SCOPE = sorted(endpoints.manifest_scopes())


def ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": data})


@pytest.fixture
def tower_ready(app: Any) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={
            "tower_url": "https://tower.example.com",
            "tower_sender_key": SecretStr("sender-key"),
            "tower_callback_secret": SecretStr("callback-secret"),
            "tower_target_keys": {tower.TARGET: SecretStr("target-key")},
        }
    )


async def owner_turn(session: Any, app: Any, *, assistant: bool) -> tuple[Any, Any, Any]:
    """本人私聊的一轮，飞书授权为「全部权限」；`assistant` 时这个机器人是他的个人助理。"""
    bot, user, task = await setup(session, app)
    if assistant:
        await into_assistant(session, bot, user)
        await keys.ensure_key(session, sealed.master(), user.id)
        await session.commit()
    await grant(
        session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=EVERY_SCOPE,
        requested_scopes=EVERY_SCOPE,
    )
    return bot, user, task


def chat(mock: respx.MockRouter) -> respx.Route:
    """李四私聊里的最近消息，以及（不该被调用的）回复接口。"""
    mock.get(f"{FEISHU}/im/v1/chats/oc_li").mock(return_value=ok({"chat_mode": "p2p"}))
    mock.get(f"{FEISHU}/im/v1/messages").mock(return_value=ok({"items": [message("om_2")]}))
    # 通讯录里按 open_id 取对方姓名。
    mock.get(url__startswith=f"{FEISHU}/contact/v3/").mock(
        return_value=ok({"user": {"name": "李四"}})
    )
    return mock.post(f"{FEISHU}/im/v1/messages/om_2/reply").mock(
        return_value=ok({"message_id": "om_sent"})
    )


async def listed(client: httpx.AsyncClient, app: Any, task: Any, user: Any) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = await client.post(URL, headers=await headers(app, task, user), json=body)
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["result"]["tools"]}


async def approvals(session: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(AssistantApproval)) or 0)


@pytest.mark.usefixtures("tower_ready")
async def test_assistant_write_is_staged_and_never_reaches_feishu(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    _, user, task = await owner_turn(db_session, app, assistant=True)
    with respx.mock as mock:
        sent = chat(mock)
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("feishu_reply_message", reply()),
            )
        )
    # 所有者本人私聊发来的一轮：暂存进本轮的确认卡，不提交审批系统，也不调飞书的写接口。
    assert out["status"] == "pending_confirmation" and out["pending"] == 1
    assert "确认卡" in out["hint"] and "不要重试" in out["hint"]
    assert not sent.called
    assert fake.submitted == [] and await approvals(db_session) == 0
    state = await db_session.scalar(
        select(InteractionState).where(InteractionState.kind == "assistant_write")
    )
    assert state is not None and state.state["mode"] == "card"
    assert [item["tool"] for item in state.state["items"]] == ["feishu_reply_message"]


@pytest.mark.usefixtures("tower_ready")
async def test_assistant_reads_run_as_before(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    _, user, task = await owner_turn(db_session, app, assistant=True)
    with respx.mock as mock:
        mock.get(f"{FEISHU}/im/v1/messages").mock(return_value=ok({"items": [message("om_2")]}))
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("feishu_chat_history", {"chat_id": "oc_li"}),
            )
        )
    assert out["items"][0]["message_id"] == "om_2"
    assert fake.submitted == [] and await approvals(db_session) == 0


async def test_without_an_approval_service_the_owner_still_confirms_on_a_card(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    """在场的确认卡不经审批系统：没配审批系统照样暂存，等所有者点卡，绝不当场执行。
    （无人在场的一轮没配审批系统时照旧失败关闭，见 test_assistant_write_batches。）"""
    _, user, task = await owner_turn(db_session, app, assistant=True)
    with respx.mock as mock:
        sent = chat(mock)
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("feishu_reply_message", reply()),
            )
        )
    assert out["status"] == "pending_confirmation"
    assert not sent.called and await approvals(db_session) == 0


@pytest.mark.usefixtures("tower_ready")
async def test_assistant_listing_marks_writes_and_keeps_file_tools(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=True)
    tools = await listed(client, app, task, user)
    for name, item in tools.items():
        staged = "approval" in item["inputSchema"].get("properties", {})
        assert staged == catalog.is_write(name), name
    assert "pending_approval" in tools["feishu_reply_message"]["description"]
    # 带文件的工具照常列出并过审批；取上传链接不改动飞书里的任何东西，原样列出。
    assert catalog.FILE_TOOLS <= set(tools)
    assert "pending_approval" in tools["feishu_send_file"]["description"]
    assert "pending_approval" not in tools["feishu_prepare_upload"]["description"]
    assert "feishu_search_messages" in tools and "feishu_authorize" in tools


@pytest.mark.usefixtures("tower_ready")
async def test_standard_bot_is_unchanged(
    client: httpx.AsyncClient, app: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通机器人不受影响：清单里没有审批相关的东西，写类工具照旧直接执行。"""
    fake = install_tower(monkeypatch)
    _, user, task = await owner_turn(db_session, app, assistant=False)
    tools = await listed(client, app, task, user)
    assert all(
        "approval" not in item["inputSchema"].get("properties", {}) for item in tools.values()
    )
    assert "pending_approval" not in json.dumps(tools) and "feishu_save_to_drive" in tools
    assert not [name for name in tools if name.startswith(("approval_", "assistant_"))]
    arguments = {k: v for k, v in reply().items() if k != "approval"}
    with respx.mock as mock:
        sent = chat(mock)
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("feishu_reply_message", arguments),
            )
        )
    assert out["message_id"] == "om_sent" and sent.called
    assert fake.submitted == [] and await approvals(db_session) == 0
    # 助理专用的入参对普通机器人是非法参数，不会被悄悄忽略。
    refused = value(
        await client.post(
            URL, headers=await headers(app, task, user), json=rpc("feishu_reply_message", reply())
        )
    )
    assert refused["error"] == "invalid_tool_or_arguments"
