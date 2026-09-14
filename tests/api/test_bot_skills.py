import uuid

from sqlalchemy import select

from coreman.core.db.models import (
    BotSkill,
    EnvPreset,
    Skill,
    SkillApproval,
    SkillSource,
    Task,
    User,
)
from coreman.core.knowledge import installation as installs
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import skill_install
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import build_ctx, seed_bot


async def prepare(client, session, *, internal=False):
    bot, relay, cipher = await seed_bot(session, env={"MANUAL_KEY": "manual"})
    relay.agent_port = 9001
    relay.agent_token_enc = cipher.encrypt("synthetic-long-token", "relay_servers.agent_token_enc")
    source = SkillSource(
        key="tools", label="Tools", git_url="https://github.com/example/skills.git"
    )
    session.add(source)
    await session.flush()
    skill = Skill(
        name="query",
        source_id=source.id,
        enabled=True,
        security_level="internal" if internal else "public",
        selectable_env_groups={"erp": "ERP"} if internal else {},
        user_env_vars={"API_KEY": {"required": True}},
        security_prompt_template="Only approved tables" if internal else None,
    )
    session.add_all(
        [
            skill,
            EnvPreset(
                group_key="erp",
                label="ERP",
                vars_enc=cipher.encrypt(
                    '{"DB_PASSWORD":"synthetic-password"}', installs.PRESET_AAD
                ),
            ),
        ]
    )
    await session.commit()
    await login_existing(client, session, await session.get(User, bot.created_by))
    return bot, skill, cipher


async def execute(session, engine, task_id):
    task = await session.get(Task, task_id)
    task.status, task.claimed_at, task.heartbeat_at = "running", utcnow(), utcnow()
    await session.commit()
    await skill_install.SkillInstallHandler().run(build_ctx(engine, task))
    await session.refresh(task)
    return task


async def test_public_install_secret_layer_and_uninstall(
    client, db_session, db_engine, monkeypatch
):
    bot, skill, cipher = await prepare(client, db_session)
    calls = []

    async def agent(relay, cipher, operation, payload):
        calls.append(payload)
        assert "API_KEY" not in str(payload)
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    path = f"/api/admin/bots/{bot.id}/skills/{skill.id}"
    result = await client.post(
        path + "/install", json={"user_env_vars": {"API_KEY": "synthetic-user-secret"}}
    )
    assert result.status_code == 200, result.text
    task_id = result.json()["data"]["task_id"]
    task = await execute(db_session, db_engine, task_id)
    assert task.status == "succeeded", task.error_message
    assert calls[0]["skill_name"] == "query"
    assert "synthetic-user-secret" not in str(task.payload)
    row = await db_session.get(BotSkill, (bot.id, skill.id))
    assert row.status == "installed"
    assert await installs.effective_env(db_session, cipher, bot) == {
        "API_KEY": "synthetic-user-secret",
        "MANUAL_KEY": "manual",
    }
    result = await client.get(f"/api/admin/bots/{bot.id}/skills")
    assert "synthetic-user-secret" not in result.text
    revision = result.json()["data"]["items"][0]["revision"]
    assert (await client.delete(path, headers={"If-Match": str(revision)})).status_code == 200
    db_session.expire_all()
    from coreman.core.db.models import Bot

    bot = await db_session.get(Bot, uuid.UUID(path.split("/")[4]))
    assert await installs.effective_env(db_session, cipher, bot) == {"MANUAL_KEY": "manual"}


async def test_internal_approval_binds_scope_actor_and_catalog(
    client, db_session, db_engine, monkeypatch
):
    bot, skill, _ = await prepare(client, db_session, internal=True)
    path = f"/api/admin/bots/{bot.id}/skills/{skill.id}/install"
    result = await client.post(
        path, json={"selected_env_groups": ["erp"], "user_env_vars": {"API_KEY": "secret"}}
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    approval_id = data["approval"]["id"]
    assert (await db_session.scalar(select(Task))) is None
    assert (
        await client.post(
            f"/api/admin/skill-approvals/{approval_id}/review",
            json={"decision": "approve"},
            headers={"If-Match": "1"},
        )
    ).status_code == 403
    reviewer = await login_as(client, db_session, role="ai_committee")
    review_path = f"/api/admin/skill-approvals/{approval_id}/review"
    assert (
        await client.post(
            review_path,
            json={"decision": "approve", "approved_databases": ["erp", "other"]},
            headers={"If-Match": "1"},
        )
    ).status_code == 422
    result = await client.post(
        review_path,
        json={"decision": "approve", "approved_databases": ["erp"]},
        headers={"If-Match": "1"},
    )
    assert result.status_code == 200, result.text
    task_id = result.json()["data"]["task_id"]
    task = await db_session.get(Task, task_id)
    assert task.user_id == reviewer.id
    skill.description = "catalog changed after approval"
    await db_session.commit()
    calls = []

    async def agent(*args):
        calls.append(args)
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    task = await execute(db_session, db_engine, task_id)
    assert task.status == "failed" and not calls
    row = await db_session.get(BotSkill, (bot.id, skill.id))
    assert row.status == "failed" and row.installed_at is None
    approval = await db_session.get(SkillApproval, uuid.UUID(approval_id))
    assert approval.status == "approved"


async def test_install_cancelled_during_agent_cannot_resurrect(
    client, db_session, db_engine, monkeypatch
):
    bot, skill, _ = await prepare(client, db_session)
    path = f"/api/admin/bots/{bot.id}/skills/{skill.id}"
    result = await client.post(path + "/install", json={"user_env_vars": {"API_KEY": "secret"}})
    task_id = result.json()["data"]["task_id"]

    async def agent(*args):
        listing = (await client.get(f"/api/admin/bots/{bot.id}/skills")).json()["data"]
        assert (
            await client.delete(path, headers={"If-Match": str(listing["items"][0]["revision"])})
        ).status_code == 200
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    task = await execute(db_session, db_engine, task_id)
    assert task.status in ("failed", "cancelled")
    row = await db_session.get(BotSkill, (bot.id, skill.id))
    assert row.status == "uninstalled" and row.installed_at is None


async def test_approved_install_policy_survives_edit_and_lost_task(
    client, db_session, db_engine, monkeypatch
):
    from coreman.core.db.models import Bot

    bot, skill, cipher = await prepare(client, db_session, internal=True)
    bot_id, skill_id = bot.id, skill.id
    path = f"/api/admin/bots/{bot_id}/skills/{skill_id}"
    body = {"selected_env_groups": ["erp"], "user_env_vars": {"API_KEY": "secret"}}
    result = await client.post(path + "/install", json=body)
    approval_id = result.json()["data"]["approval"]["id"]
    await login_as(client, db_session, role="ai_committee")
    result = await client.post(
        f"/api/admin/skill-approvals/{approval_id}/review",
        json={
            "decision": "approve",
            "approved_databases": ["erp"],
            "approved_security_prompt": "Only orders table",
        },
        headers={"If-Match": "1"},
    )
    assert result.status_code == 200, result.text

    async def agent(*args):
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    task = await execute(db_session, db_engine, result.json()["data"]["task_id"])
    assert task.status == "succeeded", task.error_message
    await db_session.refresh(bot)
    assert "Only orders table" in bot.merged_system_prompt
    assert (await installs.effective_env(db_session, cipher, bot))[
        "DB_PASSWORD"
    ] == "synthetic-password"
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.patch(
        f"/api/admin/bots/{bot_id}",
        json={"system_prompt": "updated base"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    assert (
        bot.merged_system_prompt.startswith("updated base")
        and "Only orders table" in bot.merged_system_prompt
    )
    # 失联旧安装不能把已安装的授权抹掉，也不能自动重放远端操作。
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    row.status, row.install_task_id = "installing", task.id
    task.status = "failed"
    await db_session.commit()
    assert await installs.recover_installations(db_session, utcnow()) == 1
    await db_session.commit()
    assert row.status == "failed" and row.install_task_id is None
    assert "DB_PASSWORD" in await installs.effective_env(db_session, cipher, bot)
    assert await installs.recover_installations(db_session, utcnow()) == 0
    await db_session.rollback()
    bot = await db_session.get(Bot, bot_id)
    await db_session.refresh(row)
    assert (await client.delete(path, headers={"If-Match": str(row.revision)})).status_code == 200
    await db_session.refresh(row)
    await db_session.refresh(bot)
    assert "Only orders table" not in bot.merged_system_prompt
    assert "DB_PASSWORD" not in await installs.effective_env(db_session, cipher, bot)
