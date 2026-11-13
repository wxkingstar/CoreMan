"""目标员工那一轮「以所有者的身份」收窄成：业务系统权限与已保存的个人凭证；其余一律不挂。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import dispatch_prompt
from coreman.core.cloud_proxy import policy as cloud_policy
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BusinessSystem, BusinessTokenIssue, RelayServer, RuntimeNode
from coreman.core.personal_credentials import policy as credential_policy
from coreman.core.personal_credentials import store
from coreman.core.systems_catalog import policy as catalog_policy
from coreman.runtime.worker.chat import credentials
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.credential_helpers import FIELDS, VALUES
from tests.integration.dispatch_helpers import asked, help_task, run_help, target_bot
from tests.integration.test_assistant_platform_turn_capabilities import mounted
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import MASTER, turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")
# 需要所有者当场点卡片、或以他的飞书 / 企微身份行事的入口：这一轮一个都不该有。
WITHHELD = (
    "COREMAN_FEISHU_PERSONAL_",
    "COREMAN_WECOM_PERSONAL_",
    "COREMAN_SCHEDULE_",
    "COREMAN_COLLABORATION_",
    "COREMAN_FEISHU_DOCS_",
    credential_policy.ENV_PREFIX,
    cloud_policy.ENV_PREFIX,
)


async def equipped(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[Bot, Bot, Any]:
    """助理、一位带业务系统目录的目标员工，所有者在目标员工名下存了个人凭证。"""
    install(monkeypatch, FakeBusinessSystem())
    bot, owner = await assistant(session)
    target = await target_bot(session)
    relay = await session.get(RelayServer, target.relay_server_id)
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
        session, Cipher(MASTER), bot_id=target.id, user_id=owner.id, fields=FIELDS, values=VALUES
    )
    await session.commit()
    return bot, target, owner


async def test_the_round_has_the_owners_business_access_and_saved_credentials(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, target, owner = await equipped(db_session, monkeypatch)
    task = await help_task(db_session, await asked(db_session, bot, target))
    _, relay = await run_help(db_engine, db_session, task)
    (request,) = relay.requests
    env = request["env_vars"]
    # 业务系统令牌按所有者签发，流水记在所有者名下、目标员工这一轮上。
    assert "BOT_TOKEN_STOCK" in env and env["COREMAN_USER_SUBJECT"] == "owner"
    issue = (await db_session.scalars(select(BusinessTokenIssue))).one()
    assert (issue.user_id, issue.bot_id, issue.task_id) == (owner.id, target.id, task.id)
    # 所有者在这位员工名下存过的凭证照常注入；业务系统目录照常挂（写由平台另行把关）。
    assert {key: env[key] for key in VALUES} == VALUES
    assert mounted(env) == {"catalog", "credentials"}
    # 需要他当场点卡片的入口一个都没有：个人工具、定时任务、协作、凭证索取、云签名代理。
    assert not [key for key in env if key.startswith(WITHHELD)]
    assert cloud_policy.APPROVERS_ENV in env
    system = request["messages"][0]["content"]
    for heading in ("## 本人飞书", "## 本人定时任务", "## 个人凭证"):
        assert heading not in system
    block = turn_block(request)
    assert dispatch_prompt.target_note(allow_write=False) in block
    assert credentials.stored_status(tuple(sorted(VALUES))) in block


async def test_the_note_follows_the_approved_flag(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, target, _ = await equipped(db_session, monkeypatch)
    row = await asked(db_session, bot, target, allow_write=True, approved=True)
    _, relay = await run_help(db_engine, db_session, await help_task(db_session, row))
    (request,) = relay.requests
    block = turn_block(request)
    assert dispatch_prompt.target_note(allow_write=True) in block
    assert dispatch_prompt.target_note(allow_write=False) not in block
    # 批准过也不挂云签名代理：审批套审批，结果回不到助理。
    assert not [key for key in request["env_vars"] if key.startswith(cloud_policy.ENV_PREFIX)]


async def test_an_owner_without_saved_credentials_gets_none_and_no_way_to_ask(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    task = await help_task(db_session, await asked(db_session, bot, target))
    _, relay = await run_help(db_engine, db_session, task)
    (request,) = relay.requests
    assert not [k for k in request["env_vars"] if k.startswith(credential_policy.ENV_PREFIX)]
    assert credentials.stored_status(()) in turn_block(request)
