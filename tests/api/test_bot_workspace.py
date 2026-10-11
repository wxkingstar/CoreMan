from unittest.mock import AsyncMock

import pytest

from coreman.api.routers import bot_workspace
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, Task
from coreman.core.relay.agent_client import AgentError
from tests.api.conftest import login_as
from tests.fakes.runtime_node import add_node_relay


async def setup(client, session):
    user = await login_as(client, session)
    relay = await add_node_relay(session)
    bot = Bot(
        bot_key="workspace-test",
        name="工作文件",
        platform="wecom",
        created_by=user.id,
        relay_server_id=relay.id,
        model="claude-sonnet-4-6",
        working_dir="/home/ai/workspace-test",
        credentials_enc="unused",
    )
    session.add(bot)
    await session.commit()
    return bot, f"/api/admin/bots/{bot.id}/workspace"


@pytest.fixture
def agent(monkeypatch):
    mock = AsyncMock(return_value={"success": True, "workspace_protocol": 1, "files": []})
    monkeypatch.setattr(bot_workspace, "call_agent", mock)
    return mock


async def test_employee_admin_only_even_platform_admin(client, db_session, agent):
    bot, url = await setup(client, db_session)
    assert (await client.get(url)).status_code == 200
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(url)).status_code == 403
    assert (await client.get(url + "/files")).status_code == 403
    agent.assert_not_called()


async def test_trusted_directory_and_safe_unavailable_error(client, db_session, agent):
    bot, url = await setup(client, db_session)
    result = await client.get(url + "/files", params={"path": "docs"})
    assert result.status_code == 200
    assert agent.call_args.args[3] == {
        "path": "docs",
        "working_dir": bot.working_dir,
        "bot_id": str(bot.id),
    }
    agent.side_effect = AgentError("secret-token stderr")
    result = await client.get(url + "/files")
    assert result.status_code == 502
    assert "secret-token" not in result.text


async def test_old_runtime_upgrade_and_no_relay(client, db_session, agent):
    bot, url = await setup(client, db_session)
    agent.return_value = {"success": True}
    result = await client.get(url + "/files")
    assert result.status_code == 409
    assert "升级" in result.text
    bot.relay_server_id = None
    await db_session.commit()
    assert (await client.get(url + "/files")).status_code == 409


@pytest.mark.parametrize("status", ["claimed", "running"])
async def test_active_tasks_block_write_but_not_read(client, db_session, agent, status):
    bot, url = await setup(client, db_session)
    db_session.add(Task(bot_id=bot.id, kind="chat", payload={}, status=status))
    await db_session.commit()
    result = await client.put(
        url + "/file", json={"path": "AGENTS.md", "content": "new", "expected_hash": "a" * 64}
    )
    assert result.status_code == 409
    agent.assert_not_called()
    assert (await client.get(url + "/file", params={"path": "AGENTS.md"})).status_code == 200


@pytest.mark.parametrize("state", ["migrating", "initializing", "busy", "pending"])
async def test_operation_state_blocks_write(client, db_session, agent, state):
    bot, url = await setup(client, db_session)
    bot.workspace_state = state
    await db_session.commit()
    result = await client.put(
        url + "/file", json={"path": "AGENTS.md", "content": "new", "expected_hash": "a" * 64}
    )
    assert result.status_code == 409
    agent.assert_not_called()


async def test_git_token_encrypted_and_push_required(client, db_session, agent):
    bot, url = await setup(client, db_session)
    result = await client.put(
        url + "/git",
        json={
            "git_url": "https://github.com/org/repo.git",
            "access_token": "very-secret",
        },
    )
    assert result.status_code == 200, result.text
    assert "very-secret" not in result.text
    await db_session.refresh(bot)
    assert bot.git_token_enc.startswith("enc:v2:k1:")
    assert (
        Cipher(b"\x07" * 32).decrypt(bot.git_token_enc, bot_workspace.GIT_TOKEN_AAD)
        == "very-secret"
    )
    result = await client.post(url + "/git/backup", json={"files": ["AGENTS.md"]})
    assert result.status_code == 502
    await db_session.refresh(bot)
    assert bot.git_last_backup_at is None
    agent.return_value = {"success": True, "workspace_protocol": 1, "pushed": True, "head": "abc"}
    result = await client.post(url + "/git/backup", json={"files": ["AGENTS.md"]})
    assert result.status_code == 200, result.text
    await db_session.refresh(bot)
    assert bot.git_last_backup_at is not None
    assert agent.call_args.args[3]["git_access_token"] == "very-secret"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@github.com/org/repo",
        "file:///tmp/repo",
        "http://git/repo",
        "--config=evil",
    ],
)
def test_reject_unsafe_git_urls(url):
    with pytest.raises(ValueError):
        bot_workspace.GitConfigIn(git_url=url)


async def test_hash_conflict_is_reported_without_runtime_detail(client, db_session, agent):
    _, url = await setup(client, db_session)
    agent.side_effect = [
        {"success": True, "workspace_protocol": 1},
        AgentError("private file content", code="conflict"),
    ]
    result = await client.put(
        url + "/file", json={"path": "AGENTS.md", "content": "new", "expected_hash": "a" * 64}
    )
    assert result.status_code == 409
    assert "private file content" not in result.text


async def test_git_token_preserve_clear_and_invalid_input_redaction(client, db_session):
    bot, url = await setup(client, db_session)
    config = {"git_url": "https://github.com/org/repo.git"}
    assert (
        await client.put(url + "/git", json={**config, "access_token": "secret"})
    ).status_code == 200
    await db_session.refresh(bot)
    encrypted = bot.git_token_enc
    assert (await client.put(url + "/git", json=config)).status_code == 200
    await db_session.refresh(bot)
    assert bot.git_token_enc == encrypted
    bad = await client.put(
        url + "/git",
        json={**config, "git_url": "https://secret@github.com/repo", "access_token": "secret"},
    )
    assert bad.status_code == 422
    assert "secret" not in bad.text
    assert (await client.put(url + "/git", json={**config, "access_token": ""})).status_code == 200
    await db_session.refresh(bot)
    assert bot.git_token_enc == ""


async def test_initialize_failure_persists_and_retry_recovers(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, url = await setup(client, db_session)
    mock = AsyncMock(side_effect=AgentError("Agent 未完成操作，请查看该实例状态"))
    monkeypatch.setattr(workspace_transfer, "call_agent", mock)
    failed = await client.post(url + "/initialize")
    assert failed.status_code == 502
    state = (await client.get(url)).json()["data"]
    assert state["state"] == "failed"
    mock.side_effect = None
    mock.return_value = {"success": True, "workspace_protocol": 1}
    recovered = await client.post(url + "/initialize")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["state"] == "ready"
    assert recovered.json()["data"]["error"] is None


async def test_failed_workspace_allows_instruction_repair_but_not_backup(client, db_session, agent):
    bot, url = await setup(client, db_session)
    bot.workspace_state = "failed"
    await db_session.commit()
    assert (
        await client.put(
            url + "/file",
            json={"path": "CLAUDE.md", "content": "merged", "expected_hash": "a" * 64},
        )
    ).status_code == 200
    assert (await client.post(url + "/git/backup", json={"files": []})).status_code == 409


async def test_git_default_branch_and_adopt_requires_confirmation(client, db_session, agent):
    bot, url = await setup(client, db_session)
    config = {"git_url": "https://github.com/org/repo.git"}
    assert (await client.put(url + "/git", json=config)).status_code == 200
    agent.return_value = {
        "success": True,
        "workspace_protocol": 1,
        "reachable": True,
        "branch": "trunk",
        "empty": False,
        "linked": False,
    }
    result = await client.post(url + "/git/test")
    assert result.status_code == 200, result.text
    assert "branch" not in agent.call_args.args[3]
    await db_session.refresh(bot)
    assert bot.git_branch == "trunk"

    def remote_not_empty(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        raise AgentError("refused", code="remote_not_empty")

    agent.side_effect = remote_not_empty
    result = await client.post(url + "/git/backup", json={"files": ["AGENTS.md"]})
    assert result.status_code == 409
    assert "远端仓库已有内容" in result.text

    agent.side_effect = None
    agent.reset_mock()
    assert (await client.post(url + "/git/adopt", json={})).status_code == 422
    assert (await client.post(url + "/git/adopt", json={"confirm": False})).status_code == 422
    agent.assert_not_called()
    agent.return_value = {"success": True, "workspace_protocol": 1, "branch": "trunk"}
    result = await client.post(url + "/git/adopt", json={"confirm": True})
    assert result.status_code == 200, result.text
    assert agent.call_args.args[2] == "workspace-git-adopt"
    assert agent.call_args.args[3]["content"]


async def test_git_token_from_git_tagged_preset(client, db_session, agent):
    from coreman.core.db.models import EnvPreset

    bot, url = await setup(client, db_session)
    cipher = Cipher(b"\x07" * 32)
    db_session.add_all(
        [
            EnvPreset(
                group_key="github",
                label="GitHub App",
                vars_enc=cipher.encrypt('{"GH_TOKEN":"app-token"}', "env_presets.vars_enc"),
                tags=["git"],
            ),
            EnvPreset(
                group_key="erp",
                label="ERP",
                vars_enc=cipher.encrypt('{"DB_PASSWORD":"db-secret"}', "env_presets.vars_enc"),
            ),
        ]
    )
    await db_session.commit()
    listed = (await client.get(url + "/git/presets")).json()["data"]
    assert listed == [{"group_key": "github", "label": "GitHub App", "vars": ["GH_TOKEN"]}]
    config = {"git_url": "https://github.com/org/repo.git"}
    # 没打 git 标签的预设、不存在的变量都不能用。
    for preset, var in [("erp", "DB_PASSWORD"), ("github", "MISSING")]:
        result = await client.put(
            url + "/git", json={**config, "token_preset": preset, "token_var": var}
        )
        assert result.status_code == 422, result.text
    result = await client.put(
        url + "/git", json={**config, "token_preset": "github", "token_var": "GH_TOKEN"}
    )
    assert result.status_code == 200, result.text
    assert "app-token" not in result.text
    assert result.json()["data"]["token_preset"] == "github"
    agent.return_value = {"success": True, "workspace_protocol": 1, "branch": "main"}
    assert (await client.post(url + "/git/test")).status_code == 200
    assert agent.call_args.args[3]["git_access_token"] == "app-token"
    # 改回手动填写：清掉预设，用员工自己的令牌。
    result = await client.put(url + "/git", json={**config, "access_token": "own"})
    assert result.json()["data"]["token_preset"] is None
    assert (await client.post(url + "/git/test")).status_code == 200
    assert agent.call_args.args[3]["git_access_token"] == "own"
