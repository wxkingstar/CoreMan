"""无人在场的轮次不下发以所有者身份签发的业务系统令牌，也不挂业务系统 MCP；机器人自己配的
静态环境变量照常下发，所有者亲自私聊的一轮不受影响。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.auth import system_access
from coreman.core.bots.secrets import ENV_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BusinessSystem,
    BusinessTokenIssue,
    RelayServer,
    RuntimeNode,
)
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.integration.test_assistant_platform_turn_capabilities import equipped, sent
from tests.integration.test_assistant_platform_turn_handler import Handover, queued
from tests.integration.test_assistant_unattended_result_turn import result_turn
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import MASTER, turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")
MCP_URL = "https://mcp.example.test/mcp"


async def with_systems(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Bot:
    """`equipped` 的助理（已有一个发令牌的业务系统 stock），再加一个 MCP 系统和一条静态变量。"""
    bot = await equipped(session, monkeypatch)
    relay = await session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        system_access.MCP_RUNTIME_CAPABILITY: True,
    }
    node.capabilities = capabilities
    session.add(
        BusinessSystem(
            key="lookup",
            name="Lookup",
            token_delivery="mcp",
            mcp_url=MCP_URL,
            token_audience="mcp",
            default_for_all_bots=True,
            allowed_bot_ids=None,
        )
    )
    bot.env_vars_enc = Cipher(MASTER).encrypt(json.dumps({"SHOP_REGION": "east"}), ENV_AAD)
    await session.commit()
    return bot


def tokens(env: dict[str, str]) -> set[str]:
    return {key for key in env if key.startswith("BOT_TOKEN_")}


def live_mounts(env: dict[str, str]) -> list[dict[str, Any]]:
    """这一轮真正挂上的业务系统 MCP（地址为空的是让驱动显式禁用）。"""
    return [item for item in json.loads(env.get(system_access.MCP_MOUNTS_ENV, "[]")) if item["url"]]


async def issues(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(BusinessTokenIssue)) or 0)


async def test_the_owners_turn_gets_tokens_and_the_mcp_system(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await with_systems(db_session, monkeypatch)
    task = await chat_task(
        db_session, bot, "查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    env = (await sent(db_engine, task, ChatTaskHandler()))["env_vars"]
    assert tokens(env) == {"BOT_TOKEN_STOCK", "BOT_TOKEN_LOOKUP"}
    assert live_mounts(env) == [{"key": "lookup", "url": MCP_URL}]
    assert env["SHOP_REGION"] == "east" and env["COREMAN_USER_SUBJECT"] == "owner"
    assert await issues(db_session) == 2


@pytest.mark.parametrize("kind", ["handover", "approval_result"])
async def test_an_unattended_turn_gets_neither(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    bot = await with_systems(db_session, monkeypatch)
    if kind == "handover":
        task = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问库存"})
        request = await sent(db_engine, task, Handover())
    else:
        task = await result_turn(db_session, bot)
        request = await sent(db_engine, task, AssistantApprovalResultHandler())
    env = request["env_vars"]
    assert tokens(env) == set()
    assert live_mounts(env) == []
    # 以所有者身份签发的东西一样都不在：业务系统账号、令牌配置。
    assert not {"COREMAN_USER_SUBJECT", "COREMAN_SYSTEMS", "BOT_SYSTEMS_CONFIG"} & set(env)
    assert await issues(db_session) == 0
    # 机器人自己配的静态变量不动。
    assert env["SHOP_REGION"] == "east"
    # 「业务系统访问」段仍在稳定段里（同一会话前后几轮逐字不变），本轮块说明这一轮用不了。
    assert "## 业务系统访问" in request["messages"][0]["content"]
    assert system_access.OWNER_ABSENT_NOTE in turn_block(request)
