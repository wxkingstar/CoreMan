"""平台管理命令：设置、模型、平台应用、接口调用方、审计、统计、对话记录、任务与技能补充命令。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from coreman.cli import main
from coreman.core.bots.secrets import encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    ApiClient,
    AuditLog,
    ChatLog,
    EnvPreset,
    PlatformApp,
    Setting,
    Skill,
    SkillSource,
    UserIdentity,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD
from tests.api.conftest import MASTER_KEY
from tests.api.test_admin_cli import add_user
from tests.integration.worker_helpers import seed_bot

CIPHER = Cipher(base64.b64decode(MASTER_KEY))


@pytest.fixture
def cli(api_settings, db_engine, capsys):  # type: ignore[no-untyped-def]
    """同 test_admin_cli：在另一个线程里跑 `python -m coreman.cli …`，返回退出码与输出。"""

    async def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = await asyncio.to_thread(main, list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return run


async def test_settings_alerts_and_notifications(cli, db_session, tmp_path, monkeypatch):
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")

    code, _, err = await cli("settings", "show", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, _ = await cli("settings", "defaults", "--as", "bob")
    assert code == 0 and "default_verbosity_level" in out

    code, out, _ = await cli("settings", "show", "--as", "ops")
    assert code == 0 and "session_ttl_hours" in out and "用 settings get 查看" in out
    # 数值按 JSON 解析，文本项原样保存（"123" 不会变成数字）
    prompt = tmp_path / "org.md"
    prompt.write_text("我们是一家示例公司。\n", encoding="utf-8")
    code, _, err = await cli(
        "settings",
        "set",
        "session_ttl_hours=24",
        "jwt_issuer=123",
        "default_effort_level=high",
        "--file",
        f"prompt_org_context={prompt}",
        "--as",
        "ops",
    )
    assert code == 0, err
    code, out, _ = await cli("settings", "get", "jwt_issuer", "--as", "ops")
    assert out.strip() == "123"
    code, out, _ = await cli("settings", "get", "prompt_org_context", "--as", "ops")
    assert out == "我们是一家示例公司。\n\n"
    code, out, _ = await cli("settings", "get", "session_ttl_hours", "--json", "--as", "ops")
    assert json.loads(out) == 24
    code, _, _ = await cli("settings", "set", "default_effort_level=null", "--as", "ops")
    code, out, _ = await cli("settings", "get", "default_effort_level", "--as", "ops")
    assert code == 0 and out.strip() == "null"
    code, _, err = await cli("settings", "set", "no_such_key=1", "--as", "ops")
    assert code == 1 and "没有可修改的设置项" in err
    code, _, err = await cli("settings", "set", "session_ttl_hours=abc", "--as", "ops")
    assert code == 1 and "JSON" in err

    # 通知邮箱：密码从标准输入读，不回显
    monkeypatch.setattr("sys.stdin", io.StringIO("smtp-pass-123\n"))
    code, _, err = await cli(
        "settings",
        "notifications-set",
        "--enabled",
        "on",
        "--host",
        "smtp.example.com",
        "--username",
        "robot",
        "--sender",
        "robot@example.com",
        "--password-file",
        "-",
        "--as",
        "ops",
    )
    assert code == 0, err
    # 换认证目标须重填密码：接口的校验原样报给用户
    code, _, err = await cli("settings", "notifications-set", "--port", "587", "--as", "ops")
    assert code == 1 and "重新填写密码" in err
    code, _, err = await cli(
        "settings", "notifications-set", "--security", "starttls", "--as", "ops"
    )
    assert code == 0, err
    code, out, _ = await cli("settings", "notifications", "--as", "ops")
    assert "smtp.example.com" in out and "starttls" in out and "smtp-pass-123" not in out
    row = await db_session.get(Setting, "notification_smtp")
    assert row is not None and row.value["password_enc"] and row.value["version"] == 2

    # 告警接收渠道：应用按名称、成员按登录名
    ops = await add_user(db_session, "alice", "platform_admin")
    db_session.add(UserIdentity(user_id=ops.id, platform="feishu", platform_user_id="fs_a"))
    db_session.add(
        PlatformApp(
            platform="feishu",
            name="通知应用",
            capabilities=["notify"],
            app_id="cli_demo",
            secret_enc=CIPHER.encrypt("s", "platform_apps.secret_enc"),
        )
    )
    await db_session.commit()
    code, _, err = await cli("settings", "alerts-set", "--as", "ops")
    assert code == 1 and "--clear" in err
    code, _, err = await cli("settings", "alerts-set", "--channel", "通知应用=alice", "--as", "ops")
    assert code == 0, err
    code, out, _ = await cli("settings", "alerts", "--as", "ops")
    assert "通知应用" in out and "alice" in out
    code, _, err = await cli("settings", "alerts-set", "--clear", "--as", "ops")
    assert code == 0, err
    row = await db_session.get(Setting, "alert_channels", populate_existing=True)
    assert row is not None and row.value == {"version": 2, "channels": []}


async def test_models_and_prices(cli, db_session):
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")
    code, out, _ = await cli("models", "list", "--as", "bob")
    assert code == 0 and "claude" in out
    code, _, err = await cli("models", "add", "claude", "demo-model", "--as", "bob")
    assert code == 1 and err.strip()
    code, _, err = await cli(
        "models", "add", "claude", "demo-model", "--display-name", "演示", "--xhigh", "--as", "ops"
    )
    assert code == 0, err
    code, _, err = await cli(
        "models", "set", "claude", "demo-model", "--display-name", "", "--sort", "9", "--as", "ops"
    )
    assert code == 0, err
    code, out, _ = await cli("models", "list", "--provider", "claude", "--json", "--as", "ops")
    row = next(r for r in json.loads(out) if r["model"] == "demo-model")
    assert (row["display_name"], row["sort_order"], row["supports_xhigh"]) == (None, 9, True)
    code, _, err = await cli("models", "delete", "claude", "demo-model", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, _, err = await cli("models", "delete", "claude", "demo-model", "--yes", "--as", "ops")
    assert code == 0, err

    code, _, err = await cli(
        "models", "price-set", "demo-model", "--from", "2026-01-01", "--input", "3", "--as", "ops"
    )
    assert code == 1 and "--output" in err
    args = ("--input", "3", "--output", "15", "--cache-read", "0.3", "--cache-write", "3.75")
    code, _, err = await cli(
        "models", "price-set", "demo-model", "--from", "2026-01-01", *args, "--as", "ops"
    )
    assert code == 0, err
    # 改已有生效日的价格只需给要改的那项，其余沿用
    code, _, err = await cli(
        "models", "price-set", "demo-model", "--from", "2026-01-01", "--input", "4", "--as", "ops"
    )
    assert code == 0, err
    code, out, _ = await cli("models", "prices", "--model", "demo-model", "--json", "--as", "bob")
    rows = json.loads(out)
    assert len(rows) == 1 and rows[0]["provider"] == "claude" and rows[0]["version"] == 2
    assert float(rows[0]["input_usd"]) == 4 and float(rows[0]["output_usd"]) == 15


async def test_platform_apps_keep_secrets_out_of_argv_and_output(
    cli, db_session, tmp_path, monkeypatch
):
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob", "ai_committee")
    secret = tmp_path / "secret"
    secret.write_text("app-secret-value\n", encoding="utf-8")
    base = ("--name", "企微通讯录", "--capability", "contact_sync", "--corp-id", "ww_demo")
    code, _, err = await cli(
        "apps", "create", "--platform", "wecom", *base, "--secret-file", str(secret), "--as", "bob"
    )
    assert code == 1 and err.strip()
    code, out, err = await cli(
        "apps", "create", "--platform", "wecom", *base, "--secret-file", str(secret), "--as", "ops"
    )
    assert code == 0 and "已新建平台应用" in out, err
    app = await db_session.scalar(select(PlatformApp))
    assert app is not None
    assert CIPHER.decrypt(app.secret_enc, "platform_apps.secret_enc") == "app-secret-value"

    code, out, _ = await cli("apps", "list", "--as", "ops")
    assert "企微通讯录" in out and "ww_demo" in out
    code, out, _ = await cli("apps", "show", str(app.id)[:6], "--as", "ops")
    assert "ap••••ue" in out and "app-secret-value" not in out
    # 只改名称和能力：脱敏的 Secret 回传后服务端保留原值
    monkeypatch.setattr("sys.stdin", io.StringIO("cb-token-value"))
    code, _, err = await cli(
        "apps",
        "set",
        "企微通讯录",
        "--name",
        "企微应用",
        "--capability",
        "contact_sync",
        "--capability",
        "callback",
        "--callback-token-file",
        "-",
        "--as",
        "ops",
    )
    assert code == 0, err
    await db_session.refresh(app)
    assert app.name == "企微应用" and app.capabilities == ["contact_sync", "callback"]
    assert CIPHER.decrypt(app.secret_enc, "platform_apps.secret_enc") == "app-secret-value"
    assert app.callback_token_enc is not None
    code, out, _ = await cli("apps", "runs", "企微应用", "--as", "ops")
    assert code == 0 and "还没有同步记录" in out
    code, _, err = await cli("apps", "run-show", "999", "--as", "ops")
    assert code == 1 and "不存在" in err
    code, _, err = await cli("apps", "delete", "企微应用", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, _, err = await cli("apps", "delete", "企微应用", "--yes", "--as", "ops")
    assert code == 0, err
    assert await db_session.scalar(select(PlatformApp)) is None


async def test_api_clients_and_jwt_keys(cli, db_session):
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob", "ai_committee")
    code, _, err = await cli("clients", "list", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, err = await cli(
        "clients", "create", "demo-app", "--name", "演示", "--scope", "org", "--as", "ops"
    )
    assert code == 0, err
    first = out.split("：")[-1].strip()
    assert "只显示这一次" in out and len(first) > 32
    code, _, err = await cli(
        "clients", "set", "demo-app", "--scope", "relay", "--enabled", "off", "--as", "ops"
    )
    assert code == 0, err
    row = await db_session.get(ApiClient, "demo-app")
    assert row is not None and (row.name, row.scopes, row.enabled) == ("演示", ["relay"], False)
    code, out, _ = await cli("clients", "list", "--as", "ops")
    assert "demo-app" in out and first not in out
    code, _, err = await cli("clients", "rotate-secret", "demo-app", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli("clients", "rotate-secret", "demo-app", "--yes", "--as", "ops")
    assert code == 0, err
    assert out.split("：")[-1].strip() != first

    code, _, err = await cli("clients", "jwt-rotate", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli("clients", "jwt-rotate", "--yes", "--as", "ops")
    assert code == 0 and "新 kid" in out, err
    code, out, _ = await cli("clients", "jwt-keys", "--as", "ops")
    assert code == 0 and "是" in out
    code, out, _ = await cli("clients", "token-providers", "--as", "bob")
    assert code == 0 and "builtin" in out


async def test_audit_stats_chats_and_tasks(cli, db_session):
    bot, _, _ = await seed_bot(db_session)
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")
    for i in range(3):
        db_session.add(
            AuditLog(
                actor_login="ops",
                action=f"bot.update{i}",
                target_type="bot",
                target_id=str(bot.id),
            )
        )
    db_session.add(AuditLog(actor_login="alice", action="settings.update", target_type="settings"))
    now = datetime.now(UTC)
    db_session.add(
        ChatLog(
            bot_id=bot.id,
            bot_key=bot.bot_key,
            platform="wecom",
            user_login="alice",
            user_name="爱丽丝",
            chat_type="group",
            chat_id="g1",
            session_key="g1",
            message_type="text",
            message_content="帮我查一下订单",
            response_content="已查到 3 笔",
            status="success",
            latency_ms=1200,
            input_tokens=10,
            output_tokens=5,
            request_at=now,
        )
    )
    task = WorkTask(
        title="整理周报",
        summary="汇总本周数据",
        status="running",
        platform="feishu",
        chat_id="oc_demo",
        requester_name="爱丽丝",
        assignee_bot_id=bot.id,
        brief="按模板整理",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        WorkTaskEvent(
            work_task_id=task.id, kind="assigned", actor_type="system", human_text="已分派"
        )
    )
    await db_session.commit()

    code, _, err = await cli("audit", "list", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, _ = await cli("audit", "list", "--action", "bot.", "--limit", "2", "--as", "ops")
    assert code == 0 and "bot.update2" in out and "bot.update0" not in out
    assert "共 3 条" in out
    code, out, _ = await cli("audit", "list", "--actor", "alice", "--json", "--as", "ops")
    assert [r["action"] for r in json.loads(out)] == ["settings.update"]

    code, out, err = await cli("stats", "--as", "bob")
    assert code == 0 and "销售" in out, err
    code, out, _ = await cli("stats", "--json", "--timezone", "UTC", "--as", "bob")
    assert json.loads(out)["total"]["messages"] == 1

    code, out, _ = await cli("chats", "list", "--bot", "sales_bot", "--as", "ops")
    assert code == 0 and "帮我查一下订单" in out
    log_id = await db_session.scalar(select(ChatLog.id))
    code, out, _ = await cli("chats", "show", str(log_id), "--as", "ops")
    assert code == 0 and "已查到 3 笔" in out
    # 看不见的记录一律说不存在
    code, _, err = await cli("chats", "show", str(log_id), "--as", "bob")
    assert code == 1 and "不存在" in err
    code, out, _ = await cli("chats", "stats", "--status", "success", "--as", "ops")
    assert code == 0 and "1200" in out

    code, out, _ = await cli("tasks", "list", "--status", "running", "--as", "bob")
    assert code == 0 and task.code in out and "整理周报" in out and "销售" in out
    code, out, _ = await cli("tasks", "show", task.code, "--as", "bob")
    assert code == 0 and "已分派" in out and "按模板整理" in out
    code, _, err = await cli("tasks", "show", "T-0", "--as", "bob")
    assert code == 1 and "不存在" in err


async def test_skill_create_sources_exports_and_local_install(cli, db_session, tmp_path):
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "lead", "ai_committee")
    await add_user(db_session, "bob")

    code, _, err = await cli("skills", "source-add", "tools", "--label", "工具", "--as", "bob")
    assert code == 1 and err.strip()
    token = tmp_path / "token"
    token.write_text("glpat-demo-token\n", encoding="utf-8")
    code, out, err = await cli(
        "skills",
        "source-add",
        "tools",
        "--label",
        "工具",
        "--git-url",
        "https://github.com/example/skills.git",
        "--token-file",
        str(token),
        "--category",
        "data=数据",
        "--as",
        "lead",
    )
    assert code == 0 and "source-sync" in out, err
    source = await db_session.scalar(select(SkillSource))
    assert source is not None and source.categories == {"data": "数据"}
    assert CIPHER.decrypt(source.access_token_enc, SOURCE_TOKEN_AAD) == "glpat-demo-token"
    # 只改名称：访问令牌不回传也保留
    code, _, err = await cli("skills", "source-set", "tools", "--label", "工具箱", "--as", "lead")
    assert code == 0, err
    await db_session.refresh(source)
    assert source.label == "工具箱" and source.access_token_enc is not None
    code, _, err = await cli("skills", "source-set", "tools", "--remove-token", "--as", "lead")
    assert code == 0, err
    await db_session.refresh(source)
    assert source.access_token_enc is None

    code, out, err = await cli(
        "skills", "create", "report", "--source", "tools", "--category", "data", "--as", "lead"
    )
    assert code == 0 and "停用" in out, err
    skill = await db_session.scalar(select(Skill).where(Skill.name == "report"))
    assert skill is not None and skill.enabled is False and skill.category == "data"
    code, _, err = await cli("skills", "create", "x", "--source", "nope", "--as", "lead")
    assert code == 1 and "找不到技能来源" in err

    skill.enabled = True
    skill.env_groups = ["erp"]
    db_session.add(
        EnvPreset(
            group_key="erp",
            label="ERP",
            vars_enc=encrypt_json(CIPHER, {"DB_HOST": "db.example.com"}, "env_presets.vars_enc"),
        )
    )
    await db_session.commit()
    # 明文导出只给平台管理员
    code, _, err = await cli("skills", "env-exports", "report", "--as", "lead")
    assert code == 1 and err.strip()
    code, out, err = await cli("skills", "env-exports", "report", "--as", "ops")
    assert code == 0 and "DB_HOST" in out and "db.example.com" in out, err
    assert await db_session.scalar(select(AuditLog).where(AuditLog.action == "skill.env_export"))
    code, out, err = await cli("skills", "local-install", "report", "--as", "bob")
    assert code == 0 and "http://testserver" in out and "有效期至" in out, err
