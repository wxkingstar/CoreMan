"""需要 OAuth 的 MCP 技能：按 AI 员工命名安装，由员工所在实例的 CLI 完成授权。"""

import json
import uuid

from sqlalchemy import select

from coreman.api.routers import bot_skills
from coreman.core.db.models import AuditLog, BotSkill, RelayServer, Skill, SkillSource, User
from coreman.core.knowledge import installation as installs
from coreman.core.relay.agent_client import AgentError
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import skill_install
from tests.api.conftest import login_as, login_existing
from tests.api.test_bot_skills import execute
from tests.integration.worker_helpers import seed_bot

REMOTE = {"type": "http", "url": "https://mcp.example.com/mcp"}


async def prepare(client, session, *, oauth=True, installed=True):
    bot, relay, cipher = await seed_bot(session)
    relay.agent_token_enc = cipher.encrypt("synthetic-long-token", "relay_servers.agent_token_enc")
    source = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    session.add(source)
    await session.flush()
    skill = Skill(
        name="figma",
        source_id=source.id,
        enabled=True,
        install_type="mcp",
        mcp_oauth=oauth,
        mcp_config_enc=cipher.encrypt(json.dumps(REMOTE), installs.MCP_AAD),
    )
    session.add(skill)
    await session.flush()
    if installed:
        session.add(BotSkill(bot_id=bot.id, skill_id=skill.id, status="installed"))
    await session.commit()
    await login_existing(client, session, await session.get(User, bot.created_by))
    return bot, skill, relay


def fake_node(monkeypatch, *, ping=None, fail=None):
    calls = []

    async def agent(relay, cipher, operation, payload=None):
        calls.append((operation, payload))
        if operation == "ping":
            return ping if ping is not None else {"success": True, "mcp_oauth": 1}
        if fail is not None:
            raise fail
        if operation == "mcp-login-start":
            return {
                "success": True,
                "session_id": "session-1",
                "authorization_url": "https://auth.example.com/oauth?state=s",
                "expires_in": 600,
            }
        return {"success": True}

    monkeypatch.setattr(bot_skills, "call_agent", agent)
    return calls


async def listed(client, bot):
    response = await client.get(f"/api/admin/bots/{bot.id}/skills")
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"][0]


async def test_oauth_mcp_installs_under_a_per_employee_name(
    client, db_session, db_engine, monkeypatch
):
    bot, skill, _ = await prepare(client, db_session, installed=False)
    calls = []

    async def agent(relay, cipher, operation, payload):
        calls.append(payload)
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    result = await client.post(f"/api/admin/bots/{bot.id}/skills/{skill.id}/install", json={})
    assert result.status_code == 200, result.text
    task = await execute(db_session, db_engine, result.json()["data"]["task_id"])
    assert task.status == "succeeded", task.error_message
    assert calls[0]["skill_name"] == "figma"
    assert calls[0]["server_name"] == f"figma-{bot.id.hex[:8]}"
    assert calls[0]["mcp_config"] == REMOTE


async def test_plain_mcp_keeps_the_skill_name(client, db_session, db_engine, monkeypatch):
    bot, skill, _ = await prepare(client, db_session, oauth=False, installed=False)
    calls = []

    async def agent(relay, cipher, operation, payload):
        calls.append(payload)
        return {"success": True}

    monkeypatch.setattr(skill_install, "call_agent", agent)
    result = await client.post(f"/api/admin/bots/{bot.id}/skills/{skill.id}/install", json={})
    await execute(db_session, db_engine, result.json()["data"]["task_id"])
    assert "server_name" not in calls[0]


async def test_authorize_records_who_and_where_then_revoke(client, db_session, monkeypatch):
    bot, skill, relay = await prepare(client, db_session)
    calls = fake_node(monkeypatch)
    path = f"/api/admin/bots/{bot.id}/skills/{skill.id}/mcp-login"
    row = await listed(client, bot)
    assert (row["mcp_oauth"], row["mcp_authorized"], row["mcp_authorized_by"]) == (
        True,
        False,
        None,
    )

    started = await client.post(path)
    assert started.status_code == 200, started.text
    assert started.json()["data"] == {
        "session_id": "session-1",
        "authorization_url": "https://auth.example.com/oauth?state=s",
        "expires_in": 600,
    }
    target = {"project_dir": bot.working_dir, "server_name": f"figma-{bot.id.hex[:8]}"}
    assert calls[-1] == ("mcp-login-start", target)

    done = await client.post(
        path + "/complete",
        json={"session_id": "session-1", "callback_url": "http://127.0.0.1:1/cb?code=c&state=s"},
    )
    assert done.status_code == 200, done.text
    assert calls[-1] == (
        "mcp-login-complete",
        {
            **target,
            "session_id": "session-1",
            "callback_url": "http://127.0.0.1:1/cb?code=c&state=s",
        },
    )
    row = await listed(client, bot)
    assert row["mcp_authorized"] and row["mcp_authorized_by"] == "创建者"
    stored = await db_session.get(BotSkill, (bot.id, skill.id))
    await db_session.refresh(stored)
    assert stored.mcp_authorized_relay_id == relay.id
    actions = list(
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.target_id == str(bot.id)).order_by(AuditLog.id)
        )
    )
    assert actions[-1] == "skill.mcp_authorized"
    # 回调地址里的授权码不进审计。
    diffs = await db_session.scalars(select(AuditLog.diff).where(AuditLog.target_id == str(bot.id)))
    assert "code=c" not in str(diffs.all())

    revoked = await client.delete(path)
    assert revoked.status_code == 200, revoked.text
    assert calls[-1] == ("mcp-logout", target)
    row = await listed(client, bot)
    assert not row["mcp_authorized"] and row["mcp_authorized_at"] is None


async def test_moving_to_another_runtime_needs_a_new_authorization(client, db_session):
    bot, skill, relay = await prepare(client, db_session)
    stored = await db_session.get(BotSkill, (bot.id, skill.id))
    stored.mcp_authorized_at, stored.mcp_authorized_relay_id = utcnow(), relay.id
    await db_session.commit()
    assert (await listed(client, bot))["mcp_authorized"]
    other = RelayServer(
        name="other",
        model_provider="codex",
        runtime_node_id=relay.runtime_node_id,
        is_active=True,
    )
    db_session.add(other)
    await db_session.flush()
    bot.relay_server_id = other.id
    await db_session.commit()
    row = await listed(client, bot)
    assert not row["mcp_authorized"] and row["mcp_authorized_at"] is not None


async def test_old_runtime_is_asked_to_upgrade(client, db_session, monkeypatch):
    bot, skill, _ = await prepare(client, db_session)
    calls = fake_node(monkeypatch, ping={"success": True, "workspace_protocol": 1})
    response = await client.post(f"/api/admin/bots/{bot.id}/skills/{skill.id}/mcp-login")
    assert response.status_code == 409
    assert "升级" in response.json()["message"]
    assert [operation for operation, _ in calls] == ["ping"]


async def test_node_reason_is_shown(client, db_session, monkeypatch):
    bot, skill, _ = await prepare(client, db_session)
    fake_node(
        monkeypatch,
        fail=AgentError("运行时操作失败", code="operation_failed", detail="授权已过期，请重新开始"),
    )
    response = await client.post(
        f"/api/admin/bots/{bot.id}/skills/{skill.id}/mcp-login/complete",
        json={"session_id": "session-1", "callback_url": "http://127.0.0.1:1/cb?code=c"},
    )
    assert response.status_code == 422
    assert response.json()["message"] == "授权已过期，请重新开始"
    stored = await db_session.get(BotSkill, (bot.id, skill.id))
    await db_session.refresh(stored)
    assert stored.mcp_authorized_at is None


async def test_only_installed_oauth_skills_of_own_bots(client, db_session, monkeypatch):
    calls = fake_node(monkeypatch)
    bot, skill, _ = await prepare(client, db_session, oauth=False)
    path = f"/api/admin/bots/{bot.id}/skills/{skill.id}/mcp-login"
    assert (await client.post(path)).status_code == 422
    skill.mcp_oauth = True
    stored = await db_session.get(BotSkill, (bot.id, skill.id))
    stored.status = "installing"
    await db_session.commit()
    assert (await client.post(path)).status_code == 409
    await login_as(client, db_session, login_name=f"other-{uuid.uuid4().hex[:6]}")
    assert (await client.post(path)).status_code == 403
    assert calls == []


async def test_editor_rejects_oauth_for_local_servers(admin_client, db_session):
    source = SkillSource(key="local", label="Local")
    db_session.add(source)
    await db_session.commit()
    body = {
        "name": "local-mcp",
        "source_id": str(source.id),
        "install_type": "mcp",
        "mcp_config": {"command": "npx"},
        "mcp_oauth": True,
    }
    response = await admin_client.post("/api/admin/skills", json=body)
    assert response.status_code == 422
    response = await admin_client.post("/api/admin/skills", json={**body, "mcp_config": REMOTE})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["mcp_oauth"] is True
    switched = await admin_client.put(
        f"/api/admin/skills/{response.json()['data']['id']}",
        json={
            **body,
            "install_type": "git",
            "mcp_config": None,
            "external_repo_url": "https://github.com/example/s.git",
        },
        headers={"If-Match": str(response.json()["data"]["revision"])},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["data"]["mcp_oauth"] is False
