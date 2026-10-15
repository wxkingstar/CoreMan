"""技能本地安装命令与明文环境变量导出的接口。"""

import io
import json
import tarfile
from datetime import timedelta

from sqlalchemy import select

from coreman.core.db.models import AuditLog, EnvPreset, Skill, SkillSource, User
from coreman.core.knowledge import local_install
from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as

PRESET_AAD = "env_presets.vars_enc"


def archive_of(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def seed(session, cipher, **skill_fields):
    source = SkillSource(
        key="tools",
        label="Tools",
        git_url="https://git.example.com/group/skills.git",
        access_token_enc=cipher.encrypt("source-token", SOURCE_TOKEN_AAD),
        git_username="demo-user",
    )
    session.add(source)
    await session.flush()
    fields = {"name": "report", "enabled": True, **skill_fields}
    skill = Skill(source_id=source.id, **fields)
    session.add(skill)
    await session.commit()
    return skill


def token_of(command: str) -> str:
    url = command.split()[2]
    return url.rsplit("/", 1)[1]


async def test_member_gets_command_script_and_archive(client, db_session, app, monkeypatch):
    cipher = app.state.cipher
    skill = await seed(
        db_session, cipher, external_repo_url="https://git.example.com/group/skills.git@report"
    )
    member = await login_as(client, db_session, role="member")
    result = await client.post(f"/api/admin/skills/{skill.id}/local-install")
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    command = result.json()["data"]["command"]
    assert command.startswith("curl -fsSL http://testserver/api/skill-install/")
    assert command.endswith(" | sh")
    token = token_of(command)

    # 脚本与压缩包凭命令里的令牌下载，不需要登录态。
    client.cookies.clear()
    script = await client.get(f"/api/skill-install/{token}")
    assert script.status_code == 200
    assert "name=report" in script.text
    assert f"url=http://testserver/api/skill-install/{token}/archive" in script.text

    calls = []

    def fetch(url, name, access_token=None, username=None):
        calls.append((url, name, access_token, username))
        return archive_of({"SKILL.md": "---\nname: report\n---\n"})

    monkeypatch.setattr(local_install, "fetch_skill", fetch)
    archive = await client.get(f"/api/skill-install/{token}/archive")
    assert archive.status_code == 200, archive.text
    assert archive.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(archive.content)) as opened:
        assert opened.getnames() == ["SKILL.md"]
    # 去掉 Runtime 用的 @技能名 后缀；来源令牌只交给来源自己的仓库。
    assert calls == [
        ("https://git.example.com/group/skills.git", "report", "source-token", "demo-user")
    ]
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "skill.local_install")
    )
    assert audit.actor_id == member.id and audit.target_id == str(skill.id)


async def test_command_refuses_mcp_disabled_and_stale_tokens(client, db_session, app):
    cipher = app.state.cipher
    skill = await seed(db_session, cipher)
    mcp = Skill(name="figma", source_id=skill.source_id, enabled=True, install_type="mcp")
    db_session.add(mcp)
    await db_session.commit()
    member = await login_as(client, db_session, role="member")
    assert (await client.post(f"/api/admin/skills/{mcp.id}/local-install")).status_code == 422
    command = (await client.post(f"/api/admin/skills/{skill.id}/local-install")).json()["data"]
    token = token_of(command["command"])

    expired = local_install.seal_token(
        cipher, member.id, skill.id, utcnow() - local_install.TOKEN_TTL - timedelta(seconds=1)
    )
    assert (await client.get(f"/api/skill-install/{expired}")).status_code == 410
    assert (await client.get("/api/skill-install/forged")).status_code == 404

    skill.enabled = False
    await db_session.commit()
    assert (await client.get(f"/api/skill-install/{token}/archive")).status_code == 409
    assert (await client.post(f"/api/admin/skills/{skill.id}/local-install")).status_code == 409

    skill.enabled = True
    user = await db_session.get(User, member.id)
    user.status = "disabled"
    await db_session.commit()
    assert (await client.get(f"/api/skill-install/{token}")).status_code == 403


async def test_archive_reports_missing_skill_without_git_output(
    client, db_session, app, monkeypatch
):
    skill = await seed(db_session, app.state.cipher)
    await login_as(client, db_session, role="member")
    command = (await client.post(f"/api/admin/skills/{skill.id}/local-install")).json()["data"]
    token = token_of(command["command"])

    def missing(*_args):
        raise LookupError("report")

    monkeypatch.setattr(local_install, "fetch_skill", missing)
    result = await client.get(f"/api/skill-install/{token}/archive")
    assert result.status_code == 404 and "report" in result.json()["message"]

    def denied(*_args):
        raise ValueError("fatal: secret detail")

    monkeypatch.setattr(local_install, "fetch_skill", denied)
    result = await client.get(f"/api/skill-install/{token}/archive")
    assert result.status_code == 502 and "secret detail" not in result.text


async def test_env_exports_are_platform_admin_only_plain_and_audited(client, db_session, app):
    cipher = app.state.cipher
    skill = await seed(
        db_session,
        cipher,
        env_groups=["oss"],
        selectable_env_groups={"db_erp": "ERP", "db_crm": "CRM"},
        data_sources={"mysql": "MySQL", "doris": "Doris"},
        default_data_source="mysql",
        doris_enabled_groups=["db_erp"],
        user_env_vars={"API_KEY": {"label": "个人密钥", "required": True}},
    )
    presets = {
        "oss": {"OSS_SECRET": "oss-plain"},
        "db_erp": {"DB_PASSWORD": "erp-plain"},
        "db_erp_doris": {"DB_PASSWORD": "doris-plain"},
        "db_crm": {"DB_PASSWORD": "crm-plain"},
    }
    for key, values in presets.items():
        db_session.add(
            EnvPreset(
                group_key=key, label=key, vars_enc=cipher.encrypt(json.dumps(values), PRESET_AAD)
            )
        )
    await db_session.commit()
    path = f"/api/admin/skills/{skill.id}/env-exports"
    body = {"selected_env_groups": ["db_erp"], "data_source": "doris"}

    for role in ("member", "ai_committee"):
        await login_as(client, db_session, role=role)
        assert (await client.post(path, json=body)).status_code == 403

    admin = await login_as(client, db_session, role="platform_admin")
    result = await client.post(path, json=body)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    text = result.json()["data"]["text"]
    assert "export OSS_SECRET=oss-plain" in text and "export DB_PASSWORD=doris-plain" in text
    assert "erp-plain" not in text and "# export API_KEY=''  # 个人密钥（必填）" in text

    # 两个数据库给同名变量不同的值，与员工安装一样拒绝。
    conflict = await client.post(path, json={"selected_env_groups": ["db_erp", "db_crm"]})
    assert conflict.status_code == 409 and "DB_PASSWORD" in conflict.json()["message"]
    assert (await client.post(path, json={"selected_env_groups": ["other"]})).status_code == 422

    audits = list(
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "skill.env_export"))
    )
    assert len(audits) == 1 and audits[0].actor_id == admin.id
    assert audits[0].diff == {"env_groups": [None, ["oss", "db_erp_doris"]]}
    assert "plain" not in json.dumps(audits[0].diff)
