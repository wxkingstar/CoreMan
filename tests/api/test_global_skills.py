"""全局技能：一处勾选，所有普通 AI 员工自动安装；取消全局只动随全局装上的行。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from coreman.core.db.models import (
    Bot,
    BotSkill,
    EnvPreset,
    GlobalSkill,
    Skill,
    SkillSource,
    Task,
    User,
)
from coreman.core.knowledge import global_skills
from coreman.core.knowledge import installation as installs
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import skill_install
from tests.api.conftest import login_as, login_existing
from tests.api.test_bot_skills import execute
from tests.integration.worker_helpers import seed_bot


async def prepare(client, session, monkeypatch):
    bot, relay, cipher = await seed_bot(session, env={"MANUAL_KEY": "manual"})
    relay.agent_token_enc = cipher.encrypt("synthetic-long-token", "relay_servers.agent_token_enc")
    source = SkillSource(
        key="tools", label="Tools", git_url="https://github.com/example/skills.git"
    )
    session.add(source)
    await session.flush()
    skill = Skill(name="file-share", source_id=source.id, enabled=True, env_groups=["share_files"])
    session.add_all(
        [
            skill,
            EnvPreset(
                group_key="share_files",
                label="Share",
                vars_enc=cipher.encrypt('{"SHARE_TOKEN":"synthetic-token"}', installs.PRESET_AAD),
            ),
        ]
    )
    other = Bot(
        bot_key="ops_bot",
        platform="wecom",
        name="运营",
        created_by=bot.created_by,
        relay_server_id=None,
        model=bot.model,
        working_dir="/data/skills/ops_bot",
        credentials_enc=bot.credentials_enc,
    )
    session.add(other)
    await session.commit()
    calls = []

    async def agent(relay, cipher, operation, payload):
        calls.append(payload)
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    return bot, other, skill, cipher, calls


async def toggle(client, skill_id, **body):
    return await client.put(f"/api/admin/skills/{skill_id}/global", json=body)


async def test_global_install_injects_presets_and_reaches_new_bots(
    client, db_session, db_engine, monkeypatch
):
    bot, other, skill, cipher, calls = await prepare(client, db_session, monkeypatch)
    bot_id, other_id, skill_id = bot.id, other.id, skill.id
    await login_existing(client, db_session, await _creator(db_session, bot_id))
    # 机器人管理员不是委员会成员，不能设全局。
    assert (await toggle(client, skill_id, is_global=True)).status_code == 403
    committee_id = (await login_as(client, db_session, role="ai_committee")).id
    result = await toggle(client, skill_id, is_global=True)
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    # 没分到运行时的员工先不装，由调度器之后补装。
    assert [item["bot_id"] for item in data["queued"]] == [str(bot_id)]
    assert data["skipped"] == []
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    task = await execute(db_session, db_engine, row.install_task_id)
    assert task.status == "succeeded", task.error_message
    assert calls[0]["skill_name"] == "file-share"
    db_session.expire_all()
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    assert row.status == "installed" and row.installed_globally
    bot = await db_session.get(Bot, bot_id)
    assert await installs.effective_env(db_session, cipher, bot) == {
        "SHARE_TOKEN": "synthetic-token",
        "MANUAL_KEY": "manual",
    }
    listed = (await client.get("/api/admin/skills")).json()["data"]["items"]
    assert [item["is_global"] for item in listed] == [True]
    await login_existing(client, db_session, await _creator(db_session, bot_id))
    items = (await client.get(f"/api/admin/bots/{bot_id}/skills")).json()["data"]["items"]
    assert items[0]["is_global"] and items[0]["installed_globally"]
    # 不能单独卸载，也不能直接从目录删除。
    revision = items[0]["revision"]
    result = await client.delete(
        f"/api/admin/bots/{bot_id}/skills/{skill_id}", headers={"If-Match": str(revision)}
    )
    assert result.status_code == 409
    await login_as(client, db_session, role="ai_committee")
    skill = await db_session.get(Skill, skill_id)
    result = await client.delete(
        f"/api/admin/skills/{skill_id}", headers={"If-Match": str(skill.revision)}
    )
    assert result.status_code == 409
    # 新员工分到运行时后，调度器补装；已装的不重复排。
    other = await db_session.get(Bot, other_id)
    other.relay_server_id = bot.relay_server_id
    await db_session.commit()
    assert await global_skills.sweep(db_session, cipher) == 1
    await db_session.commit()
    assert await global_skills.sweep(db_session, cipher) == 0
    row = await db_session.get(BotSkill, (other_id, skill_id))
    assert row.status == "installing" and row.installed_globally
    task = await db_session.get(Task, row.install_task_id)
    assert task.user_id == committee_id


async def test_global_rejects_skills_needing_per_bot_setup(client, db_session, monkeypatch):
    _, _, skill, _, _ = await prepare(client, db_session, monkeypatch)
    await login_as(client, db_session, role="platform_admin")
    skill.security_level = "internal"
    await db_session.commit()
    result = await toggle(client, skill.id, is_global=True)
    assert result.status_code == 422 and "审批" in result.text
    skill.security_level, skill.user_env_vars = "public", {"API_KEY": {"required": True}}
    await db_session.commit()
    result = await toggle(client, skill.id, is_global=True)
    assert result.status_code == 422 and "参数" in result.text
    assert await db_session.get(GlobalSkill, skill.id) is None


async def test_manual_install_kept_and_unglobal_uninstalls_auto_rows(
    client, db_session, db_engine, monkeypatch
):
    bot, other, skill, cipher, _ = await prepare(client, db_session, monkeypatch)
    bot_id, other_id, skill_id = bot.id, other.id, skill.id
    other.relay_server_id, other.created_by = bot.relay_server_id, bot.created_by
    await db_session.commit()
    # 先给 bot 手动装上：设全局时不动它，取消全局时也保留。
    await login_existing(client, db_session, await _creator(db_session, bot_id))
    result = await client.post(f"/api/admin/bots/{bot_id}/skills/{skill_id}/install", json={})
    assert result.status_code == 200, result.text
    task = await execute(db_session, db_engine, result.json()["data"]["task_id"])
    assert task.status == "succeeded", task.error_message
    await login_as(client, db_session, role="ai_committee")
    data = (await toggle(client, skill_id, is_global=True)).json()["data"]
    assert [item["bot_id"] for item in data["queued"]] == [str(other_id)]
    db_session.expire_all()
    assert not (await db_session.get(BotSkill, (bot_id, skill_id))).installed_globally
    row = await db_session.get(BotSkill, (other_id, skill_id))
    task = await execute(db_session, db_engine, row.install_task_id)
    assert task.status == "succeeded", task.error_message
    data = (await toggle(client, skill_id, is_global=False)).json()["data"]
    assert [item["bot_id"] for item in data["uninstalled"]] == [str(other_id)]
    db_session.expire_all()
    assert (await db_session.get(BotSkill, (bot_id, skill_id))).status == "installed"
    row = await db_session.get(BotSkill, (other_id, skill_id))
    assert row.status == "uninstalled" and not row.installed_globally
    assert await db_session.get(GlobalSkill, skill_id) is None


async def test_unglobal_keep_installed_and_failed_retry(client, db_session, db_engine, monkeypatch):
    bot, _, skill, cipher, _ = await prepare(client, db_session, monkeypatch)
    bot_id, skill_id = bot.id, skill.id
    await login_as(client, db_session, role="ai_committee")
    await toggle(client, skill_id, is_global=True)
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    row.status, row.install_task_id = "failed", None
    await db_session.commit()
    # 自动安装失败后，不到一小时不重试；满一小时由调度器重试。
    assert await global_skills.sweep(db_session, cipher) == 0
    row.updated_at = utcnow() - timedelta(hours=2)
    await db_session.commit()
    assert await global_skills.sweep(db_session, cipher) == 1
    await db_session.commit()
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    task = await execute(db_session, db_engine, row.install_task_id)
    assert task.status == "succeeded", task.error_message
    data = (await toggle(client, skill_id, is_global=False, keep_installed=True)).json()["data"]
    assert data["uninstalled"] == []
    db_session.expire_all()
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    assert row.status == "installed" and not row.installed_globally


async def test_worker_refuses_global_install_after_unglobal(
    client, db_session, db_engine, monkeypatch
):
    bot, _, skill, _, calls = await prepare(client, db_session, monkeypatch)
    await login_as(client, db_session, role="ai_committee")
    await toggle(client, skill.id, is_global=True)
    row = await db_session.get(BotSkill, (bot.id, skill.id))
    task_id = row.install_task_id
    # 绕过接口直接删掉全局标记，模拟取消全局与安装执行并发：委员会成员不是机器人管理员，应被拒。
    await db_session.delete(await db_session.get(GlobalSkill, skill.id))
    await db_session.commit()
    task = await execute(db_session, db_engine, task_id)
    assert task.status == "failed" and calls == []
    assert "管理员" in (task.error_message or "")


async def _creator(session, bot_id):
    bot = await session.get(Bot, bot_id)
    return await session.scalar(select(User).where(User.id == bot.created_by))


async def test_global_and_companion_sources_coexist(client, db_session, monkeypatch):
    from coreman.core.db.models import BusinessSystem

    bot, _, skill, _, _ = await prepare(client, db_session, monkeypatch)
    bot_id, skill_id = bot.id, skill.id
    db_session.add(
        BusinessSystem(
            key="lookup",
            name="Lookup",
            token_delivery="mcp",
            mcp_url="https://mcp.example.test/mcp",
            companion_skill_id=skill_id,
            allowed_bot_ids=[bot_id],
        )
    )
    await db_session.commit()
    await login_as(client, db_session, role="ai_committee")
    await toggle(client, skill_id, is_global=True)
    row = await db_session.get(BotSkill, (bot_id, skill_id))
    row.status, row.install_task_id, row.installed_at = "installed", None, utcnow()
    await db_session.commit()
    # 授权系统：已随全局装上，只记上系统来源。
    await login_existing(client, db_session, await _creator(db_session, bot_id))
    path = f"/api/admin/bots/{bot_id}/system-grants"
    version = (await db_session.get(Bot, bot_id)).version
    r = await client.put(path, json={"system_keys": ["lookup"]}, headers={"If-Match": str(version)})
    assert r.status_code == 200, r.text
    row = await db_session.get(BotSkill, (bot_id, skill_id), populate_existing=True)
    assert row.installed_by_system == "lookup" and row.installed_globally
    # 取消全局：系统仍授权着，保留。
    await login_as(client, db_session, role="ai_committee")
    data = (await toggle(client, skill_id, is_global=False)).json()["data"]
    assert data["uninstalled"] == []
    row = await db_session.get(BotSkill, (bot_id, skill_id), populate_existing=True)
    assert row.status == "installed" and not row.installed_globally
    # 再设全局后撤销系统授权：仍是全局技能，只去掉系统来源。
    row.installed_globally = True
    await db_session.commit()
    await toggle(client, skill_id, is_global=True)
    await login_existing(client, db_session, await _creator(db_session, bot_id))
    r = await client.put(
        path, json={"system_keys": []}, headers={"If-Match": str(r.json()["data"]["version"])}
    )
    assert r.status_code == 200, r.text
    row = await db_session.get(BotSkill, (bot_id, skill_id), populate_existing=True)
    assert row.status == "installed" and row.installed_by_system is None
