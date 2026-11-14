"""无人在场的轮次照常以所有者身份签发业务系统令牌、挂业务系统 MCP，供只读调研，本轮块写明只读；
冷启动归纳口吻的那一轮什么都不挂。机器人自己配的静态环境变量照常下发，所有者亲自私聊的一轮
不受影响。"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings, voice
from coreman.core.assistants.kinds import (
    APPROVAL_RESULT_KIND,
    COLD_START_KIND,
    ORIGIN_KINDS,
)
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
from coreman.runtime.worker.assistant_cold_start import AssistantColdStartHandler
from coreman.runtime.worker.chat import credentials
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.integration.credential_helpers import VALUES
from tests.integration.test_assistant_platform_turn_capabilities import equipped, mounted, sent
from tests.integration.test_assistant_platform_turn_handler import Handover, queued
from tests.integration.test_assistant_unattended_result_turn import result_turn
from tests.integration.test_chat_handler import chat_task
from tests.integration.test_cron_handler import claim
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


def subject_of(token: str) -> Any:
    """内置签发方的 JWT 里写的主体（只解出载荷，不验签：这里只看签给了谁）。"""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]


async def issued_to(session: AsyncSession) -> set[tuple[str, Any]]:
    rows = await session.scalars(select(BusinessTokenIssue))
    return {(row.subject, row.user_id) for row in rows}


async def unattended_turn(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, kind: str
) -> dict[str, Any]:
    """跑一轮这个类型的无人在场任务，返回发给运行时的请求。

    审批结果回合用真实的（带着提交那一轮的入站事件）；冷启动用真实的口吻样本那一轮；其余
    平台来源的类型走同一个处理器基类，开轮时的能力只看任务类型。
    """
    if kind == APPROVAL_RESULT_KIND:
        return await sent(engine, await result_turn(session, bot), AssistantApprovalResultHandler())
    if kind == COLD_START_KIND:
        row = await settings.get(session, bot.id)
        samples = ["收到，我看一下", "可以，周四下班前给你", "这个先放一放", "辛苦了", "明天看"]
        assert await voice.enqueue(session, bot, row, samples, 1763017200000) is not None
        await session.commit()
        return await sent(engine, await claim(session), AssistantColdStartHandler())
    handler = type("Turn", (Handover,), {"kind": kind})()
    return await sent(engine, await queued(session, bot, kind, {"note": "李四问库存"}), handler)


@pytest.mark.parametrize("kind", [kind for kind in ORIGIN_KINDS if kind != COLD_START_KIND])
async def test_an_unattended_turn_can_look_things_up_as_the_owner(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    bot = await with_systems(db_session, monkeypatch)
    request = await unattended_turn(db_engine, db_session, bot, kind)
    env = request["env_vars"]
    assert tokens(env) == {"BOT_TOKEN_STOCK", "BOT_TOKEN_LOOKUP"}
    assert live_mounts(env) == [{"key": "lookup", "url": MCP_URL}]
    # 签给所有者本人：平台来源的轮次没有入站的发言者，发言者固定为所有者。
    assert env["COREMAN_USER_SUBJECT"] == "owner"
    assert subject_of(env["BOT_TOKEN_STOCK"]) == "owner"
    assert await issued_to(db_session) == {("owner", bot.owner_id)}
    assert await issues(db_session) == 2
    # 云签名代理与操作目录也挂上（写由服务端拒绝）；所有者的个人凭证仍不给。
    assert mounted(env) == {"cloud", "catalog"}
    assert not set(VALUES.values()) & set(env.values())
    assert env["SHOP_REGION"] == "east"
    block = turn_block(request)
    assert system_access.OWNER_ABSENT_NOTE in block
    assert credentials.OWNER_ABSENT in block
    # 「确认后可写」那一句不出现：这一轮没有人能确认。
    assert system_access.ENV_WRITE_REMINDER not in block


async def test_the_cold_start_turn_gets_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归纳口吻用不到业务系统：最小授权，以所有者身份签发的东西一样都不在。"""
    bot = await with_systems(db_session, monkeypatch)
    request = await unattended_turn(db_engine, db_session, bot, COLD_START_KIND)
    env = request["env_vars"]
    assert tokens(env) == set()
    assert live_mounts(env) == []
    assert not {"COREMAN_USER_SUBJECT", "COREMAN_SYSTEMS", "BOT_SYSTEMS_CONFIG"} & set(env)
    assert await issues(db_session) == 0
    assert mounted(env) == set()
    assert not set(VALUES.values()) & set(env.values())
    # 机器人自己配的静态变量不动。
    assert env["SHOP_REGION"] == "east"
    # 「业务系统访问」段仍在稳定段里，本轮块说明这一轮用不了。
    assert "## 业务系统访问" in request["messages"][0]["content"]
    block = turn_block(request)
    assert system_access.WITHHELD_NOTE in block
    assert system_access.OWNER_ABSENT_NOTE not in block
