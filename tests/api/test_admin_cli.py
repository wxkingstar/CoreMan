"""管理命令：以指定成员身份经管理 API 执行，权限、审计与管理后台一致。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.cli import main
from coreman.core.bots.secrets import ENV_AAD, decrypt_json
from coreman.core.db.models import (
    AdminSession,
    AuditLog,
    Bot,
    BotAllowedUser,
    BotMember,
    BotSkill,
    BotSystemGrant,
    BusinessSystem,
    EnvPreset,
    Skill,
    SkillApproval,
    SkillSource,
    Team,
    User,
)
from coreman.core.knowledge import installation as installs
from tests.integration.worker_helpers import seed_bot


@pytest.fixture
def cli(api_settings, db_engine, capsys):  # type: ignore[no-untyped-def]
    """在另一个线程里跑 `python -m coreman.cli …`（它自己 asyncio.run），返回退出码与输出。"""

    async def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = await asyncio.to_thread(main, list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return run


async def add_user(session: AsyncSession, login: str, role: str = "member", **kw: object) -> User:
    user = User(
        login_name=login,
        display_name=kw.pop("display_name", login),
        email=f"{login}@example.test",
        role=role,
        source="sync",
        **kw,
    )
    session.add(user)
    await session.commit()
    return user


async def test_requires_an_active_actor_and_revokes_its_session(cli, db_session):
    code, _, err = await cli("teams", "list")
    assert code == 1 and "--as" in err
    await add_user(db_session, "gone", status="disabled")
    code, _, err = await cli("teams", "list", "--as", "gone")
    assert code == 1 and "不存在或已停用" in err
    await add_user(db_session, "ops", "platform_admin")
    code, out, err = await cli("teams", "list", "--as", "ops")
    assert code == 0, err
    # stdout 只有结果，服务日志不混进来
    assert '"event"' not in out
    sessions = list(await db_session.scalars(select(AdminSession)))
    assert [s.auth_method for s in sessions] == ["cli"]
    assert sessions[0].revoked_at is not None


async def test_bot_commands_keep_permissions_audit_and_masked_secrets(cli, db_session):
    bot, _, cipher = await seed_bot(db_session, env={"API_TOKEN": "keep-me-secret"})
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "alice", display_name="爱丽丝")
    await add_user(db_session, "bob")

    code, out, _ = await cli("bots", "list", "--as", "ops")
    assert code == 0 and "sales_bot" in out and "启用" in out
    # 普通成员不能停用别人的 AI 员工：和管理后台同一条权限判定
    code, _, err = await cli("bots", "disable", "sales_bot", "--as", "bob")
    assert code == 1 and err.strip()
    code, out, _ = await cli("bots", "disable", "销售", "--as", "ops")
    assert code == 0 and "已停用" in out
    code, out, _ = await cli("bots", "disable", "sales_bot", "--as", "ops")
    assert code == 0 and "已经是停用状态" in out

    # 改配置只有创建者和协作者能做（平台管理员也不行），命令行沿用同一规则
    code, _, err = await cli("bots", "set", "sales_bot", "--name", "x", "--as", "ops")
    assert code == 1 and "没有权限" in err
    code, _, err = await cli(
        "bots", "set", "sales_bot", "--name", "销售助手", "--verbosity", "2", "--as", "creator"
    )
    assert code == 0, err
    code, _, err = await cli("bots", "env-set", "sales_bot", "NEW_KEY=a=b", "--as", "creator")
    assert code == 0, err
    await db_session.refresh(bot)
    assert (bot.name, bot.verbosity_level, bot.enabled) == ("销售助手", 2, False)
    # 未改动的变量靠脱敏值回传保留原值
    assert decrypt_json(cipher, bot.env_vars_enc, ENV_AAD) == {
        "API_TOKEN": "keep-me-secret",
        "NEW_KEY": "a=b",
    }
    code, out, _ = await cli("bots", "env", "sales_bot", "--as", "creator")
    assert "NEW_KEY" in out and "keep-me-secret" not in out
    code, _, _ = await cli("bots", "env-unset", "sales_bot", "API_TOKEN", "--as", "creator")
    await db_session.refresh(bot)
    assert code == 0 and decrypt_json(cipher, bot.env_vars_enc, ENV_AAD) == {"NEW_KEY": "a=b"}

    assert (await cli("bots", "member-add", "sales_bot", "爱丽丝", "--as", "ops"))[0] == 0
    assert [m.user_id for m in await db_session.scalars(select(BotMember))] == [
        (await db_session.scalar(select(User.id).where(User.login_name == "alice")))
    ]
    assert (await cli("bots", "allowed-add", "sales_bot", "alice", "bob", "--as", "creator"))[
        0
    ] == 0
    assert len(list(await db_session.scalars(select(BotAllowedUser)))) == 2
    code, _, err = await cli(
        "bots", "allowed-remove", "sales_bot", "alice", "bob", "--as", "creator"
    )
    assert code == 1 and "allowed-clear" in err
    assert (await cli("bots", "allowed-remove", "sales_bot", "bob", "--as", "creator"))[0] == 0
    assert len(list(await db_session.scalars(select(BotAllowedUser)))) == 1

    code, _, err = await cli("bots", "delete", "sales_bot", "--as", "ops")
    assert code == 1 and "--yes" in err
    assert await db_session.get(Bot, bot.id) is not None

    actions = {
        (a.action, a.actor_login)
        for a in await db_session.scalars(select(AuditLog).where(AuditLog.target_type == "bot"))
    }
    assert {("bot.toggle", "ops"), ("bot.update", "creator"), ("bot.member_add", "ops")} <= actions


async def test_internal_skill_install_goes_through_approval(cli, db_session):
    bot, _, cipher = await seed_bot(db_session)
    source = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    db_session.add(source)
    await db_session.flush()
    db_session.add_all(
        [
            Skill(
                name="query",
                source_id=source.id,
                enabled=True,
                security_level="internal",
                selectable_env_groups={"erp": "ERP", "crm": "CRM"},
            ),
            EnvPreset(
                group_key="erp",
                label="ERP",
                vars_enc=cipher.encrypt('{"DB_PASSWORD":"synthetic"}', installs.PRESET_AAD),
            ),
        ]
    )
    await db_session.commit()
    await add_user(db_session, "reviewer", "ai_committee")

    code, out, err = await cli(
        "bots", "skill-install", "sales_bot", "query", "--env-group", "erp", "--as", "creator"
    )
    assert code == 0 and "已提交审批" in out, err
    approval = await db_session.scalar(select(SkillApproval))
    # 申请人不是审批角色：看不到审批列表
    code, _, err = await cli("approvals", "list", "--as", "creator")
    assert code == 1
    code, out, _ = await cli("approvals", "list", "--as", "reviewer")
    assert str(approval.id)[:8] in out and "销售" in out and "创建者" in out
    code, out, err = await cli("approvals", "approve", str(approval.id)[:8], "--as", "reviewer")
    assert code == 0 and "已批准" in out, err
    await db_session.refresh(approval)
    assert approval.status == "approved" and approval.approved_databases == ["erp"]
    code, _, err = await cli("approvals", "reject", str(approval.id), "--as", "reviewer")
    assert code == 1 and "approved" in err
    row = await db_session.get(BotSkill, (bot.id, approval.skill_id))
    assert row is not None and row.status != "uninstalled"


async def test_skill_catalog_and_preset_keep_unchanged_secrets(cli, db_session):
    _, _, cipher = await seed_bot(db_session)
    source = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    db_session.add(source)
    await db_session.flush()
    skill = Skill(name="report", source_id=source.id, enabled=True, description="旧描述")
    db_session.add_all(
        [
            skill,
            EnvPreset(
                group_key="erp",
                label="ERP",
                vars_enc=cipher.encrypt('{"DB_PASSWORD":"synthetic"}', installs.PRESET_AAD),
            ),
        ]
    )
    await db_session.commit()
    await add_user(db_session, "ops", "ai_committee")

    assert (await cli("skills", "disable", "report", "--as", "ops"))[0] == 0
    code, _, err = await cli("skills", "set", "report", "--category", "数据", "--as", "ops")
    assert code == 0, err
    await db_session.refresh(skill)
    assert (skill.enabled, skill.category, skill.description) == (False, "数据", "旧描述")
    code, out, _ = await cli("skills", "list", "--status", "disabled", "--as", "ops")
    assert "report" in out and "tools" in out

    code, _, err = await cli("skills", "preset-set", "erp", "--var", "DB_HOST=db", "--as", "ops")
    assert code == 0, err
    preset = await db_session.get(EnvPreset, "erp")
    await db_session.refresh(preset)
    values = decrypt_json(cipher, preset.vars_enc, installs.PRESET_AAD)
    assert values == {"DB_PASSWORD": "synthetic", "DB_HOST": "db"}


async def test_system_scope_and_grants(cli, db_session):
    bot, _, _ = await seed_bot(db_session)
    await add_user(db_session, "ops", "platform_admin")

    code, out, err = await cli("systems", "create", "crm", "--name", "CRM", "--as", "ops")
    assert code == 0 and "不开放" in out, err
    code, _, err = await cli("bots", "system-grant", "sales_bot", "crm", "--as", "creator")
    assert code == 1  # 尚未开放给这个员工
    assert (await cli("systems", "allow", "crm", "sales_bot", "--as", "ops"))[0] == 0
    code, out, err = await cli(
        "bots", "system-grant", "sales_bot", "crm", "--write", "--as", "creator"
    )
    assert code == 0 and "crm（可写）" in out, err
    grant = await db_session.scalar(select(BotSystemGrant))
    assert grant.allow_write and grant.bot_id == bot.id

    code, _, err = await cli("systems", "set", "crm", "--description", "客户", "--as", "ops")
    assert code == 0, err
    system = await db_session.get(BusinessSystem, "crm")
    await db_session.refresh(system)
    # 整份替换只改了给出的字段，白名单原样保留
    assert system.description == "客户" and system.allowed_bot_ids == [bot.id]
    # 不再开放 = 同时收回授权（服务端规则）
    assert (await cli("systems", "disallow", "crm", "销售", "--as", "ops"))[0] == 0
    assert await db_session.scalar(select(BotSystemGrant)) is None
    code, _, err = await cli("systems", "allow-all", "crm", "--as", "ops")
    assert code == 1 and "--yes" in err


async def test_teams_and_users(cli, db_session):
    await add_user(db_session, "ops", "ai_committee")
    target = await add_user(db_session, "dave")

    code, _, err = await cli("teams", "create", "sales", "--name", "销售部", "--as", "ops")
    assert code == 0, err
    code, _, err = await cli("teams", "rule-add", "销售部", "/公司/销售", "--as", "ops")
    assert code == 0, err
    assert (await cli("teams", "rule-add", "sales", "/公司/销售", "--as", "ops"))[0] == 1
    code, _, err = await cli("teams", "set", "sales", "--name-en", "Sales", "--as", "ops")
    assert code == 0, err
    team = await db_session.scalar(select(Team).where(Team.slug == "sales"))
    assert (team.name_zh, team.name_en) == ("销售部", "Sales")

    code, _, err = await cli("users", "set", "dave", "--team", "sales", "--as", "ops")
    assert code == 0, err
    code, out, _ = await cli("teams", "members", "sales", "--as", "ops")
    assert "dave" in out
    assert (await cli("users", "disable", "dave@example.test", "--as", "ops"))[0] == 0
    await db_session.refresh(target)
    assert (target.team_id, target.status) == (team.id, "disabled")
    assert "team_id" in target.manual_fields

    code, _, err = await cli("teams", "delete", "sales", "--yes", "--as", "ops")
    assert code == 1 and "仍有用户" in err
    assert (await cli("teams", "rule-remove", "sales", "/公司/销售", "--as", "ops"))[0] == 0


async def test_raw_api_reaches_other_admin_endpoints(cli, db_session):
    await add_user(db_session, "ops", "platform_admin")
    code, out, err = await cli(
        "api", "post", "/api/admin/teams", "--data", '{"slug":"x1","name_zh":"X"}', "--as", "ops"
    )
    assert code == 0 and '"slug": "x1"' in out, err
    code, out, _ = await cli("api", "get", "/api/admin/teams", "--as", "ops")
    assert '"x1"' in out
    code, _, err = await cli(
        "api", "put", f"/api/admin/teams/{uuid.uuid4()}", "--data", "{", "--as", "ops"
    )
    assert code == 1 and "JSON" in err
