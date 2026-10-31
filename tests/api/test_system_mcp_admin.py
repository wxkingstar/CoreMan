"""Admin side of MCP-delivered business systems: validation and companion skills."""

from __future__ import annotations

from sqlalchemy import select

from coreman.core.db.models import (
    AuditLog,
    BotSkill,
    BusinessSystem,
    Skill,
    SkillSource,
    Task,
    User,
)
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import seed_bot

MCP_URL = "https://mcp.example.test/mcp"


async def skill_of(session, name: str = "lookup-guide", **fields) -> Skill:
    source = SkillSource(
        key=f"src-{name}", label="Src", git_url="https://github.com/example/skills.git"
    )
    session.add(source)
    await session.flush()
    skill = Skill(name=name, source_id=source.id, enabled=True, **fields)
    session.add(skill)
    await session.commit()
    return skill


async def test_mcp_delivery_validation(client, db_session) -> None:
    await login_as(client, db_session, role="platform_admin")
    guide = await skill_of(db_session)
    internal = await skill_of(db_session, "internal-guide", security_level="internal")
    needs_input = await skill_of(
        db_session, "input-guide", user_env_vars={"API_KEY": {"required": True}}
    )
    body = {"key": "lookup", "name": "Lookup", "token_delivery": "mcp", "allowed_bot_ids": []}
    for bad in (
        {},
        {"mcp_url": "http://mcp.example.test/mcp"},
        {"mcp_url": MCP_URL, "base_url": MCP_URL, "openapi_url": MCP_URL + "/spec"},
        {"mcp_url": MCP_URL, "companion_skill_id": str(internal.id)},
        {"mcp_url": MCP_URL, "companion_skill_id": str(needs_input.id)},
        {
            "mcp_url": MCP_URL,
            "companion_skill_id": str(guide.id),
            "default_for_all_bots": True,
        },
    ):
        r = await client.post("/api/admin/systems", json=body | bad)
        assert r.status_code == 422, (bad, r.text)
    r = await client.post(
        "/api/admin/systems",
        json={**body, "key": "erp", "token_delivery": "env", "mcp_url": MCP_URL},
    )
    assert r.status_code == 422

    created = await client.post(
        "/api/admin/systems",
        json=body | {"mcp_url": MCP_URL, "companion_skill_id": str(guide.id)},
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["mcp_url"] == MCP_URL and data["companion_skill_id"] == str(guide.id)
    # 旧管理台不传两个新字段：保留库里的值。
    r = await client.put(
        "/api/admin/systems/lookup",
        json={"name": "Lookup 2", "token_delivery": "mcp", "allowed_bot_ids": []},
        headers={"If-Match": str(data["version"])},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["mcp_url"] == MCP_URL
    assert r.json()["data"]["companion_skill_id"] == str(guide.id)
    created_diff = await db_session.scalar(
        select(AuditLog.diff).where(AuditLog.action == "system.create")
    )
    assert created_diff["companion_skill_id"] == [None, str(guide.id)]


async def test_grant_installs_and_revoke_uninstalls_the_companion(client, db_session) -> None:
    bot, _, _ = await seed_bot(db_session)
    guide = await skill_of(db_session)
    kept = await skill_of(db_session, "kept-guide")
    db_session.add_all(
        [
            BusinessSystem(
                key="lookup",
                name="Lookup",
                token_delivery="mcp",
                mcp_url=MCP_URL,
                companion_skill_id=guide.id,
                allowed_bot_ids=[bot.id],
            ),
            BusinessSystem(
                key="other",
                name="Other",
                token_delivery="mcp",
                mcp_url=MCP_URL,
                companion_skill_id=kept.id,
                allowed_bot_ids=[bot.id],
            ),
            # 员工自己装过的技能：授权、撤销都不动它。
            BotSkill(bot_id=bot.id, skill_id=kept.id, status="installed"),
        ]
    )
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    path = f"/api/admin/bots/{bot.id}/system-grants"

    r = await client.put(
        path, json={"system_keys": ["lookup", "other"]}, headers={"If-Match": str(bot.version)}
    )
    assert r.status_code == 200, r.text
    row = await db_session.get(BotSkill, (bot.id, guide.id), populate_existing=True)
    assert row is not None and row.status == "installing"
    assert row.installed_by_system == "lookup"
    task = await db_session.get(Task, row.install_task_id)
    assert task is not None and task.kind == "skill_install"
    manual = await db_session.get(BotSkill, (bot.id, kept.id), populate_existing=True)
    assert manual is not None and manual.install_task_id is None

    r = await client.put(
        path, json={"system_keys": []}, headers={"If-Match": str(r.json()["data"]["version"])}
    )
    assert r.status_code == 200, r.text
    row = await db_session.get(BotSkill, (bot.id, guide.id), populate_existing=True)
    assert row is not None and row.status == "uninstalled"
    manual = await db_session.get(BotSkill, (bot.id, kept.id), populate_existing=True)
    assert manual is not None and manual.status == "installed"
    diffs = list(
        (
            await db_session.execute(
                select(AuditLog.diff).where(AuditLog.action == "bot.system_grants")
            )
        ).scalars()
    )
    assert any(d.get("companion_installed") == [None, [str(guide.id)]] for d in diffs)
    assert any(d.get("companion_uninstalled") == [None, [str(guide.id)]] for d in diffs)


async def test_shared_companion_stays_while_another_grant_needs_it(client, db_session) -> None:
    bot, _, _ = await seed_bot(db_session)
    guide = await skill_of(db_session)
    db_session.add_all(
        [
            BusinessSystem(
                key=key,
                name=key,
                token_delivery="mcp",
                mcp_url=MCP_URL,
                companion_skill_id=guide.id,
                allowed_bot_ids=[bot.id],
            )
            for key in ("first", "second")
        ]
    )
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    path = f"/api/admin/bots/{bot.id}/system-grants"
    r = await client.put(
        path, json={"system_keys": ["first", "second"]}, headers={"If-Match": str(bot.version)}
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        path,
        json={"system_keys": ["second"]},
        headers={"If-Match": str(r.json()["data"]["version"])},
    )
    assert r.status_code == 200, r.text
    row = await db_session.get(BotSkill, (bot.id, guide.id), populate_existing=True)
    assert row is not None and row.status == "installing"
