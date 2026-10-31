"""Business systems served as an MCP server: per-turn tokens, mounts and prompt lines."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import (
    MCP_MOUNTS_ENV,
    MCP_RUNTIME_CAPABILITY,
    build_system_access,
    mcp_runtime_supported,
)
from coreman.core.auth.tokens import verify_token
from coreman.core.db.models import BusinessSystem, RuntimeNode, User
from coreman.core.prompting.system_prompt import Speaker
from tests.integration.worker_helpers import seed_bot

MCP_URL = "https://mcp.example.test/mcp"


async def setup(db_session: AsyncSession, *, provider: str = "builtin"):
    bot, relay, cipher = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    assert creator
    db_session.add_all(
        [
            BusinessSystem(
                key="lookup",
                name="Lookup",
                token_delivery="mcp",
                mcp_url=MCP_URL,
                token_provider=provider,
                token_audience="mcp",
                default_for_all_bots=True,
                allowed_bot_ids=None,
            ),
            BusinessSystem(
                key="erp",
                name="ERP",
                base_url="https://erp.example.test",
                default_for_all_bots=True,
                allowed_bot_ids=None,
            ),
        ]
    )
    await db_session.commit()
    speaker = Speaker(
        creator.login_name or "", creator.id, creator.login_name, creator.display_name
    )
    return bot, relay, cipher, speaker


async def test_mcp_system_is_mounted_with_the_speakers_token(db_session: AsyncSession) -> None:
    bot, _, cipher, speaker = await setup(db_session)
    access = await build_system_access(
        db_session,
        cipher,
        bot=bot,
        speaker=speaker,
        issuer="coreman",
        external_key=None,
        mcp_supported=True,
    )
    token = access.env["COREMAN_SYSTEM_MCP_TOKEN_LOOKUP"]
    claims = await verify_token(db_session, token, issuer="coreman", audience="mcp")
    assert claims["sub"] == access.subject
    assert "BOT_TOKEN_LOOKUP" not in access.env and "BOT_TOKEN_ERP" in access.env
    assert json.loads(access.env[MCP_MOUNTS_ENV]) == [{"key": "lookup", "url": MCP_URL}]
    configs = {c["key"]: c for c in json.loads(access.env["COREMAN_SYSTEMS"])}
    # 模型拿到服务名，拿不到令牌变量名，只能走 MCP 工具。
    assert configs["lookup"]["mcp_server"] == "system_lookup"
    assert "env_var" not in configs["lookup"]
    assert "MCP 服务 `system_lookup`" in access.prompt
    assert [record.system_key for record in access.issued] == ["erp", "lookup"]


async def test_mcp_system_is_unavailable_on_old_runtimes(db_session: AsyncSession) -> None:
    bot, _, cipher, speaker = await setup(db_session)
    access = await build_system_access(
        db_session, cipher, bot=bot, speaker=speaker, issuer="coreman", external_key=None
    )
    assert not any(key.startswith("COREMAN_SYSTEM_MCP") for key in access.env)
    assert [c["key"] for c in json.loads(access.env["COREMAN_SYSTEMS"])] == ["erp"]
    assert "Lookup: 无法调用" in access.prompt
    assert [record.system_key for record in access.issued] == ["erp"]


async def test_failed_issue_disables_the_mount(db_session: AsyncSession) -> None:
    bot, _, cipher, speaker = await setup(db_session, provider="missing")
    access = await build_system_access(
        db_session,
        cipher,
        bot=bot,
        speaker=speaker,
        issuer="coreman",
        external_key=None,
        mcp_supported=True,
    )
    assert "COREMAN_SYSTEM_MCP_TOKEN_LOOKUP" not in access.env
    assert json.loads(access.env[MCP_MOUNTS_ENV]) == [{"key": "lookup", "url": ""}]
    assert "Lookup" in access.note


async def test_unknown_speaker_gets_no_mounts(db_session: AsyncSession) -> None:
    bot, _, cipher, _ = await setup(db_session)
    access = await build_system_access(
        db_session,
        cipher,
        bot=bot,
        speaker=Speaker("unknown", None, None, None),
        issuer="coreman",
        external_key=None,
        mcp_supported=True,
    )
    assert not access.env


@pytest.mark.parametrize("declared", [True, False])
async def test_runtime_support_follows_the_node_capability(
    db_session: AsyncSession, declared: bool
) -> None:
    bot, relay, _, _ = await setup(db_session)
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities = {"claude": {"installed": True, MCP_RUNTIME_CAPABILITY: declared}}
    await db_session.commit()
    assert await mcp_runtime_supported(db_session, relay=relay, model=bot.model) is declared
    # 能力按后端区分：Claude 声明了不代表 Codex 也能挂。
    node.capabilities = {"codex": {"installed": True, MCP_RUNTIME_CAPABILITY: True}}
    await db_session.commit()
    assert not await mcp_runtime_supported(db_session, relay=relay, model=bot.model)
