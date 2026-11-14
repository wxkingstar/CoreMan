"""平台来源的一轮无人在场：云签名代理与业务系统目录照常挂上（只读由服务端把关），所有者的个人
凭证不挂；私聊一轮照旧。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.cloud_proxy import policy as cloud_policy
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BusinessSystem, RelayServer, RuntimeNode, Task
from coreman.core.personal_credentials import policy as credential_policy
from coreman.core.personal_credentials import store
from coreman.core.systems_catalog import policy as catalog_policy
from coreman.runtime.worker.chat import credentials
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.fakes.fake_relay import FakeRelay
from tests.integration.credential_helpers import FIELDS, VALUES
from tests.integration.test_assistant_platform_turn_handler import Handover, assistant, queued
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import MASTER, build_ctx, turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")


async def equipped(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Bot:
    """一位助理：节点能挂业务系统目录，有一个带目录的业务系统，所有者存了个人凭证。"""
    install(monkeypatch, FakeBusinessSystem())
    bot, owner = await assistant(session)
    relay = await session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        catalog_policy.RUNTIME_CAPABILITY: True,
    }
    node.capabilities = capabilities
    session.add(
        BusinessSystem(
            key="stock",
            name="库存",
            base_url=BASE_URL,
            openapi_url=SPEC_URL,
            default_for_all_bots=True,
            allowed_bot_ids=None,
        )
    )
    await store.save(
        session, Cipher(MASTER), bot_id=bot.id, user_id=owner.id, fields=FIELDS, values=VALUES
    )
    await session.commit()
    return bot


async def sent(engine: AsyncEngine, task: Task, handler: ChatTaskHandler) -> dict[str, Any]:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    ctx.cloud_proxy_ready = True
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)
    (request,) = relay.requests
    return request


def mounted(env: dict[str, str]) -> set[str]:
    """这一轮挂上了哪几样。"""
    found = set()
    if cloud_policy.URL_ENV in env or cloud_policy.TOKEN_ENV in env:
        found.add("cloud")
    if catalog_policy.URL_ENV in env or catalog_policy.TOKEN_ENV in env:
        found.add("catalog")
    if any(key.startswith(credential_policy.ENV_PREFIX) for key in env) or set(VALUES) & set(env):
        found.add("credentials")
    return found


async def test_the_owners_turn_has_all_three(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await equipped(db_session, monkeypatch)
    task = await chat_task(
        db_session, bot, "查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    request = await sent(db_engine, task, ChatTaskHandler())
    assert mounted(request["env_vars"]) == {"cloud", "catalog", "credentials"}


async def test_a_platform_turn_has_the_proxies_but_not_the_credentials(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await equipped(db_session, monkeypatch)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    request = await sent(db_engine, task, Handover())
    env = request["env_vars"]
    assert mounted(env) == {"cloud", "catalog"}
    assert not set(VALUES.values()) & set(env.values())
    # 找谁审批的名单与个人工具照常下发。
    assert cloud_policy.APPROVERS_ENV in env and "COREMAN_FEISHU_PERSONAL_TOKEN" in env
    # 规则仍在稳定段里（同一个会话里前后几轮逐字不变），本轮块说明这一轮用不了。
    assert "## 个人凭证" in request["messages"][0]["content"]
    assert credentials.OWNER_ABSENT in turn_block(request)
