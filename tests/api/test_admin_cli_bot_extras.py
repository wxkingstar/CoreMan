"""AI 员工的其余管理命令：新建与扫码、记忆、工作目录、换运行时、协作、旁听、飞书应用等。"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from sqlalchemy import select

from coreman.api.routers import bot_memories, bot_skills, bot_workspace
from coreman.api.routers import feishu_apps as feishu_routes
from coreman.api.routers import health_report as health_report_routes
from coreman.cli import main
from coreman.core.bots import switch_relay as switch_domain
from coreman.core.bots import workspace_transfer
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.db.models import (
    Bot,
    BotApprover,
    BotHumanPartner,
    BotSkill,
    FeishuAppRegistration,
    GroupObserveDecision,
    Memory,
    Skill,
    SkillSource,
    User,
    UserIdentity,
)
from coreman.core.feishu_apps import management, registration
from coreman.core.relay import agent_client
from coreman.core.relay.sse import FinishEvent, TextDelta
from tests.api.test_admin_cli import add_user
from tests.api.test_feishu_app_management import CIPHER, Feishu
from tests.api.test_feishu_app_registrations import URL, FakeFeishu
from tests.fakes.runtime_node import add_node_relay
from tests.integration.test_human_collaboration import setup as human_setup
from tests.integration.worker_helpers import seed_bot

MODEL = "claude-sonnet-5-5"


@pytest.fixture
def cli(api_settings, db_engine, capsys):  # type: ignore[no-untyped-def]
    """同 test_admin_cli：在另一个线程里跑命令，返回退出码与输出。"""

    async def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = await asyncio.to_thread(main, list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return run


async def test_create_with_credentials_or_scan(cli, db_session, monkeypatch):
    fake = FakeFeishu()
    monkeypatch.setattr(registration, "begin", fake.begin)
    monkeypatch.setattr(registration, "poll", fake.poll)
    monkeypatch.setattr(management, "ensure_default_commands", fake.ensure_default_commands)
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "loner")  # 没有团队的普通成员不能建

    base = ["--platform", "feishu", "--model", MODEL, "--as"]
    code, _, err = await cli(
        "bots", "create", "x1", "--name", "x", "--working-dir", "/data/skills/x1",
        "--cred", "app_id=cli_x", *base, "loner",
    )  # fmt: skip
    assert code == 1 and err.strip()
    code, out, err = await cli(
        "bots", "create", "manual_bot", "--name", "手填", "--working-dir", "/data/skills/m",
        "--cred", "app_id=cli_manual", "--cred", "app_secret=s", "--env", "A=1", *base, "ops",
    )  # fmt: skip
    assert code == 0 and "已新建 手填（manual_bot）" in out, err

    fake.results.append(registration.Poll("succeeded", "cli_new", "app-secret", "ou_x"))
    code, out, err = await cli(
        "bots", "create", "scan_bot", "--name", "扫码", "--working-dir", "/data/skills/s",
        "--scan", *base, "ops",
    )  # fmt: skip
    assert code == 0, err
    assert URL in out and "已新建 扫码（scan_bot）" in out
    assert fake.begins[0]["name"] == "扫码"
    bot = await db_session.scalar(select(Bot).where(Bot.bot_key == "scan_bot"))
    assert bot is not None and fake.default_commands == ["cli_new"]
    # 标识重复：扫码前的试建就拦下，不会白建一个飞书应用
    code, _, err = await cli(
        "bots", "create", "scan_bot", "--name", "扫码", "--working-dir", "/data/skills/s2",
        "--scan", *base, "ops",
    )  # fmt: skip
    assert code == 1 and "已存在" in err and len(fake.begins) == 1

    code, out, err = await cli("bots", "feishu-scan-start", "--no-reuse", "--as", "ops")
    assert code == 0 and URL in out, err
    row = await db_session.scalar(
        select(FeishuAppRegistration).where(FeishuAppRegistration.status == "pending")
    )
    assert row is not None
    code, out, _ = await cli("bots", "feishu-scan-status", str(row.id), "--as", "ops")
    assert "pending" in out
    code, out, _ = await cli("bots", "feishu-scan-cancel", str(row.id), "--as", "ops")
    assert code == 0 and "已取消" in out


async def test_memory_commands(cli, db_session, monkeypatch, tmp_path):
    bot, _, _ = await seed_bot(db_session)
    await add_user(db_session, "ops", "platform_admin")
    source = tmp_path / "m.md"
    source.write_text("记住：周一开会", encoding="utf-8")

    code, _, err = await cli("bots", "memories", "sales_bot", "--as", "ops")
    assert code == 1 and "管理员" in err
    code, out, err = await cli(
        "bots", "memory-add", "sales_bot", "meeting.md", str(source), "--name", "例会",
        "--as", "creator",
    )  # fmt: skip
    assert code == 0 and "meeting.md" in out, err
    code, out, _ = await cli("bots", "memories", "sales_bot", "--as", "creator")
    assert "meeting.md" in out and "例会" in out
    code, out, _ = await cli("bots", "memory-show", "sales_bot", "meeting.md", "--as", "creator")
    assert "周一开会" in out
    code, _, err = await cli(
        "bots", "memory-set", "sales_bot", "meeting.md", "--type", "project", "--as", "creator"
    )
    assert code == 0, err
    row = await db_session.scalar(select(Memory))
    assert row is not None
    await db_session.refresh(row)
    assert (row.name, row.type, row.content) == ("例会", "project", "记住：周一开会")

    code, _, err = await cli("bots", "memory-delete", "sales_bot", "meeting.md", "--as", "creator")
    assert code == 1 and "--yes" in err
    # id 前缀也能定位
    code, _, err = await cli(
        "bots", "memory-delete", "sales_bot", str(row.id)[:8], "--yes", "--as", "creator"
    )
    assert code == 0, err
    code, out, _ = await cli("bots", "memories", "sales_bot", "--as", "creator")
    assert "没有记忆" in out
    code, out, _ = await cli("bots", "memories", "sales_bot", "--deleted", "--as", "creator")
    assert "meeting.md" in out

    async def agent(relay: Any, cipher: Any, operation: str, payload: Any = None) -> Any:
        return {"success": True, "count": 3}

    monkeypatch.setattr(bot_memories, "call_agent", agent)
    code, out, _ = await cli("bots", "memory-collect", "sales_bot", "--as", "creator")
    assert code == 0 and "3 个文件" in out
    code, _, err = await cli("bots", "memory-deploy", "sales_bot", "--as", "creator")
    assert code == 1 and "--yes" in err


async def test_workspace_commands(cli, db_session, monkeypatch, tmp_path):
    bot, _, _ = await seed_bot(db_session)
    bot.workspace_state = "ready"
    await db_session.commit()
    await add_user(db_session, "ops", "platform_admin")
    calls: list[tuple[str, dict[str, Any]]] = []
    content = "# 规则\n".encode()
    digest = "a" * 64

    async def agent(relay: Any, cipher: Any, operation: str, payload: Any = None) -> Any:
        calls.append((operation, payload or {}))
        if operation == "ping":
            return {"workspace_protocol": 1}
        if operation == "workspace-list":
            return {
                "entries": [{"name": "AGENTS.md", "path": "AGENTS.md", "type": "file", "size": 9}]
            }
        if operation == "workspace-read":
            data = content[payload["offset"] : payload["offset"] + payload["length"]]
            return {
                "data": base64.b64encode(data).decode(),
                "content": None,
                "size": len(content),
                "hash": digest,
                "editable": True,
                "eof": payload["offset"] + len(data) >= len(content),
            }
        if operation == "workspace-write":
            return {"hash": "b" * 64, "size": len(payload["content"])}
        if operation == "workspace-git-status":
            return {
                "initialized": True,
                "branch": "main",
                "files": [{"path": "a.py", "status": "M"}],
            }
        return {"path": payload["path"], "diff": "+new line", "truncated": False}

    monkeypatch.setattr(bot_workspace, "call_agent", agent)
    code, _, err = await cli("bots", "workspace-files", "sales_bot", "--as", "ops")
    assert code == 1 and "管理员" in err
    code, out, _ = await cli("bots", "workspace", "sales_bot", "--as", "creator")
    assert "/data/skills/sales_bot" in out and "ready" in out
    code, out, _ = await cli("bots", "workspace-files", "sales_bot", "--as", "creator")
    assert "AGENTS.md" in out
    code, out, _ = await cli("bots", "workspace-cat", "sales_bot", "AGENTS.md", "--as", "creator")
    assert code == 0 and out == "# 规则\n"
    source = tmp_path / "new.md"
    source.write_text("新内容", encoding="utf-8")
    code, out, err = await cli(
        "bots", "workspace-write", "sales_bot", "AGENTS.md", str(source), "--as", "creator"
    )
    assert code == 0 and "已写入" in out, err
    assert calls[-1] == (
        "workspace-write",
        {
            "path": "AGENTS.md",
            "content": "新内容",
            "expected_hash": digest,
            "working_dir": bot.working_dir,
            "bot_id": str(bot.id),
        },
    )
    code, _, _ = await cli(
        "bots", "workspace-write", "sales_bot", "x.md", str(source), "--new", "--as", "creator"
    )
    assert code == 0 and calls[-1][1]["expected_hash"] is None
    code, out, _ = await cli("bots", "workspace-git-status", "sales_bot", "--as", "creator")
    assert "main" in out and "a.py" in out
    code, out, _ = await cli("bots", "workspace-git-diff", "sales_bot", "a.py", "--as", "creator")
    assert "+new line" in out


async def test_switch_relay_previews_first_and_approvers(cli, db_session, monkeypatch):
    bot, _, _ = await seed_bot(db_session, model=MODEL)
    target = await add_node_relay(db_session, name="node-2")
    await db_session.commit()
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "alice", display_name="爱丽丝")
    await add_user(db_session, "bob")

    async def not_configured(*args: object, **kwargs: object) -> tuple[str, bool, set[str]]:
        return "not_configured", False, set()

    async def agent(relay: Any, cipher: Any, operation: str, payload: Any = None) -> Any:
        return {"success": True, "workspace_protocol": 1, "exists": False, "empty": True}

    monkeypatch.setattr(switch_domain, "prepare_switch", not_configured)
    monkeypatch.setattr(agent_client, "call_agent", agent)
    monkeypatch.setattr(workspace_transfer, "call_agent", agent)

    code, out, err = await cli("bots", "switch-relay", "sales_bot", "node-2", "--as", "creator")
    assert code == 0 and "--yes" in out and "目标目录" in out, err
    await db_session.refresh(bot)
    assert bot.relay_server_id != target.id
    # 同一运行时只换模型：不预览目录，同样要 --yes 才执行
    code, out, _ = await cli(
        "bots", "switch-relay", "sales_bot", "node-1", "--model", "claude-opus-5-5",
        "--as", "creator",
    )  # fmt: skip
    assert code == 0 and "--yes" in out
    code, _, err = await cli(
        "bots", "switch-relay", "sales_bot", "node-1", "--model", "claude-opus-5-5", "--yes",
        "--as", "bob",
    )  # fmt: skip
    assert code == 1 and err.strip()
    code, out, err = await cli(
        "bots", "switch-relay", "sales_bot", "node-1", "--model", "claude-opus-5-5", "--yes",
        "--as", "creator",
    )  # fmt: skip
    assert code == 0 and "claude-opus-5-5" in out, err
    await db_session.refresh(bot)
    assert bot.model == "claude-opus-5-5"

    code, _, _ = await cli("bots", "approvers-set", "sales_bot", "alice", "--as", "ops")
    assert code == 1
    code, out, err = await cli("bots", "approvers-set", "sales_bot", "爱丽丝", "--as", "creator")
    assert code == 0 and "爱丽丝" in out, err
    code, out, _ = await cli("bots", "approvers", "sales_bot", "--as", "creator")
    assert "alice" in out
    code, out, _ = await cli("bots", "approvers-set", "sales_bot", "--as", "creator")
    assert "由发起人自己确认" in out
    assert list(await db_session.scalars(select(BotApprover))) == []


async def test_human_collaborators_bot_help_and_observe(cli, db_session, monkeypatch):
    feishu = Feishu()
    monkeypatch.setattr(feishu_routes, "client_for", feishu.client)
    bot, _, helper, partner, _ = await human_setup(db_session)
    await add_user(db_session, "ops", "platform_admin")

    code, _, err = await cli("bots", "humans", "sales_bot", "--as", "ops")
    assert code == 1 and err.strip()
    code, out, _ = await cli("bots", "humans", "sales_bot", "--as", "creator")
    assert "库存专家" in out and "负责库存口径" in out
    code, out, _ = await cli(
        "bots", "human-options", "sales_bot", "--keyword", "提问", "--as", "creator"
    )
    assert "asker" in out
    code, out, err = await cli(
        "bots", "human-add", "sales_bot", "asker", "--responsibility", "审批", "--as", "creator"
    )
    assert code == 0 and "提问人" in out, err
    code, _, err = await cli(
        "bots", "human-set", "sales_bot", "expert", "--enabled", "off", "--as", "creator"
    )
    assert code == 0, err
    await db_session.refresh(partner)
    assert partner.enabled is False
    code, _, err = await cli("bots", "human-remove", "sales_bot", "expert", "--as", "creator")
    assert code == 1 and "--yes" in err
    code, _, err = await cli(
        "bots", "human-remove", "sales_bot", "expert", "--yes", "--as", "creator"
    )
    assert code == 0, err
    await db_session.refresh(partner)
    assert partner.archived is True
    rows = list(await db_session.scalars(select(BotHumanPartner.user_id)))
    assert helper.id in rows and len(rows) == 2

    code, out, err = await cli("bots", "bot-help", "sales_bot", "--as", "creator")
    assert code == 0 and "允许其他 AI 员工求助" in out, err
    code, out, err = await cli("bots", "bot-help-set", "sales_bot", "off", "--as", "creator")
    assert code == 0, err
    await db_session.refresh(bot)
    assert bot.accept_bot_help is False

    db_session.add_all(
        [
            GroupObserveDecision(
                bot_id=bot.id,
                chat_id="oc_1",
                mode="shadow",
                outcome="interject",
                state={"recent_messages": [{"sender": "提问人", "text": "库存多少"}]},
            ),
            GroupObserveDecision(bot_id=bot.id, chat_id="oc_1", mode="shadow", outcome="skip"),
        ]
    )
    await db_session.commit()
    code, out, _ = await cli(
        "bots", "observe-decisions", "sales_bot", "--would-speak", "--messages", "--as", "creator"
    )
    assert "interject" in out and "skip" not in out.split("\n\n", 1)[1] and "库存多少" in out
    decision = await db_session.scalar(
        select(GroupObserveDecision).where(GroupObserveDecision.outcome == "interject")
    )
    assert decision is not None
    code, _, err = await cli(
        "bots", "observe-review", "sales_bot", str(decision.id), "should_not", "--as", "creator"
    )
    assert code == 0, err
    await db_session.refresh(decision)
    assert decision.review == "should_not"
    code, _, err = await cli("bots", "observe-set", "sales_bot", "--as", "creator")
    assert code == 1 and "没有要修改" in err
    code, _, err = await cli(
        "bots", "observe-set", "sales_bot", "--scope", "库存问题", "--as", "creator"
    )
    assert code == 0, err
    await db_session.refresh(bot)
    assert bot.group_observe_scope == "库存问题"


async def test_feishu_app_commands(cli, db_session, monkeypatch, tmp_path):
    feishu = Feishu()
    monkeypatch.setattr(feishu_routes, "client_for", feishu.client)
    feishu.routes[("GET", management.SLASH)] = (
        200,
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "command_id": "7",
                        "command": "new",
                        "description": {"default_value": "新会话"},
                    }
                ]
            },
        },
    )
    feishu.routes[("POST", management.AVATAR_UPLOAD)] = (
        200,
        {"code": 0, "data": {"url": "https://example.com/a.png"}},
    )
    ops = await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")
    db_session.add(
        Bot(
            bot_key="fs_bot",
            platform="feishu",
            name="飞书助手",
            created_by=ops.id,
            model="m",
            working_dir="/w",
            credentials_enc=encrypt_json(
                CIPHER, {"app_id": "cli_bound", "app_secret": "s"}, CREDENTIALS_AAD
            ),
        )
    )
    await db_session.commit()

    code, _, err = await cli("bots", "feishu-app", "fs_bot", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, err = await cli("bots", "feishu-app", "fs_bot", "--as", "ops")
    assert code == 0 and "cli_bound" in out and "销售助手" in out, err

    def sent(method: str, path: str) -> dict[str, Any]:
        found = [c for c in feishu.calls if c.method == method and c.url.path.endswith(path)]
        return json.loads(found[-1].content) if found[-1].content else {}

    code, _, err = await cli(
        "bots", "feishu-app-base", "fs_bot", "--description", "卖货助手", "--as", "ops"
    )
    assert code == 0, err
    assert '"卖货助手"' in json.dumps(sent("PATCH", "/base"), ensure_ascii=False)
    code, _, err = await cli("bots", "feishu-app-bot", "fs_bot", "--no-menu", "--as", "ops")
    assert code == 0, err
    assert sent("PATCH", "/ability")["bot"]["bot_menu_enable"] is False
    avatar = tmp_path / "a.png"
    avatar.write_bytes(b"\x89PNG fake")
    code, out, err = await cli("bots", "feishu-app-avatar", "fs_bot", str(avatar), "--as", "ops")
    assert code == 0 and "已上传" in out, err
    code, _, err = await cli("bots", "feishu-app-avatar", "fs_bot", str(tmp_path), "--as", "ops")
    assert code == 1 and "PNG" in err
    code, _, err = await cli("bots", "feishu-app-scopes-apply", "fs_bot", "--as", "ops")
    assert code == 0, err
    code, _, err = await cli(
        "bots", "feishu-app-publish", "fs_bot", "--changelog", "改名", "--as", "ops"
    )
    assert code == 1 and "--yes" in err
    code, out, err = await cli(
        "bots", "feishu-app-publish", "fs_bot", "--changelog", "改名", "--yes", "--as", "ops"
    )
    assert code == 0 and "1.0.0" in out, err

    code, out, _ = await cli("bots", "feishu-commands", "fs_bot", "--as", "ops")
    assert "/new" in out and "新会话" in out
    code, _, err = await cli("bots", "feishu-command-add", "fs_bot", "/sum", "总结", "--as", "ops")
    assert code == 0 and sent("POST", management.SLASH)["command"] == "sum", err
    code, _, err = await cli(
        "bots", "feishu-command-set", "fs_bot", "new", "--description", "开新会话", "--as", "ops"
    )
    assert code == 0, err
    assert "开新会话" in json.dumps(sent("PATCH", "/7"), ensure_ascii=False)
    code, _, err = await cli("bots", "feishu-command-delete", "fs_bot", "new", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, _, err = await cli(
        "bots", "feishu-command-delete", "fs_bot", "new", "--yes", "--as", "ops"
    )
    assert code == 0 and any(c.method == "DELETE" for c in feishu.calls), err
    code, out, _ = await cli("bots", "observe-permission", "fs_bot", "--as", "ops")
    assert code == 0 and "feishu-app-scan" in out


async def test_health_report_and_mcp_login(cli, db_session, monkeypatch):
    bot, relay, cipher = await seed_bot(db_session)
    source = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    db_session.add(source)
    await db_session.flush()
    skill = Skill(
        name="figma", source_id=source.id, enabled=True, install_type="mcp", mcp_oauth=True
    )
    db_session.add(skill)
    await db_session.flush()
    db_session.add(BotSkill(bot_id=bot.id, skill_id=skill.id, status="installed"))
    await db_session.commit()

    async def agent(relay: Any, cipher: Any, operation: str, payload: Any = None) -> Any:
        if operation == "ping":
            return {"success": True, "mcp_oauth": 1}
        if operation == "mcp-login-start":
            return {
                "session_id": "session-1",
                "authorization_url": "https://auth.example.com/oauth",
                "expires_in": 600,
            }
        return {"success": True}

    monkeypatch.setattr(bot_skills, "call_agent", agent)
    code, out, err = await cli("bots", "mcp-login", "sales_bot", "figma", "--as", "creator")
    assert code == 0 and "https://auth.example.com/oauth" in out and "session-1" in out, err
    code, out, err = await cli(
        "bots", "mcp-login-complete", "sales_bot", "figma", "session-1",
        "https://localhost/callback?code=x", "--as", "creator",
    )  # fmt: skip
    assert code == 0 and "已完成" in out, err
    row = await db_session.get(BotSkill, (bot.id, skill.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.mcp_authorized_at is not None
    code, out, _ = await cli("bots", "mcp-logout", "sales_bot", "figma", "--as", "creator")
    assert code == 0 and "已退出" in out

    creator = await db_session.get(User, bot.created_by)
    assert creator is not None
    creator.source = "sync"
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="c1"))
    await db_session.commit()

    class Relay:
        async def chat_stream(self, request: Any, **kwargs: Any) -> Any:
            yield TextDelta("工作目录正常。")
            yield FinishEvent("stop")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(health_report_routes, "make_client", lambda _: Relay())
    code, out, err = await cli("bots", "health-report", "sales_bot", "--as", "creator")
    assert code == 0 and "工作目录正常。" in out, err

    bot.relay_server_id = None
    await db_session.commit()
    code, _, err = await cli("bots", "health-report", "sales_bot", "--as", "creator")
    assert code == 1 and "尚未分配" in err
