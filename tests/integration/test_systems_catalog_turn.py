"""A real chat turn: the catalog MCP reaches the runtime only when its node can mount it."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import BusinessSystem, RuntimeNode, User, UserIdentity
from coreman.core.systems_catalog import policy
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import MASTER, seed_bot


@pytest.mark.parametrize("declared", [True, False])
async def test_chat_turn_mounts_catalog_for_capable_runtimes(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    declared: bool,
) -> None:
    install(monkeypatch, FakeBusinessSystem())
    bot, relay, _ = await seed_bot(db_session)
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities = {"claude": {"installed": True, policy.RUNTIME_CAPABILITY: declared}}
    member = User(login_name="zhangsan", display_name="张三", email="zhangsan@example.test")
    db_session.add_all(
        [
            member,
            BusinessSystem(
                key="stock",
                name="库存",
                base_url=BASE_URL,
                openapi_url=SPEC_URL,
                default_for_all_bots=True,
                allowed_bot_ids=None,
            ),
        ]
    )
    await db_session.flush()
    db_session.add(UserIdentity(user_id=member.id, platform="wecom", platform_user_id="zs"))
    await db_session.commit()
    fake = FakeRelay("normal")
    task = await chat_task(db_session, bot, "查一下库存单据", sender="zs")
    await run(db_engine, task, fake)
    env = fake.requests[0]["env_vars"]
    assert "BOT_TOKEN_STOCK" in env
    if declared:
        assert env[policy.URL_ENV] == "http://localhost" + policy.API_PATH
        capability = policy.read_capability(Cipher(MASTER), env[policy.TOKEN_ENV])
        assert (capability.task_id, capability.actor) == (task.id, member.id)
    else:
        assert policy.URL_ENV not in env and policy.TOKEN_ENV not in env
