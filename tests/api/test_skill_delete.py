"""目录删除技能：在用时拒绝；删除只留墓碑，同步不再导入，同名新建即恢复。"""

import uuid

from sqlalchemy import select

from coreman.core.db.models import AuditLog, BotSkill, Skill, SkillApproval, User
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as, login_existing
from tests.api.test_bot_skills import prepare


async def test_delete_refuses_skill_in_use_then_hides_it_everywhere(client, db_session):
    bot, skill, _ = await prepare(client, db_session)
    creator = await db_session.get(User, bot.created_by)
    bot_id, skill_id, creator_id = bot.id, skill.id, creator.id
    path = f"/api/admin/skills/{skill_id}"
    assert (await client.delete(path, headers={"If-Match": "1"})).status_code == 403

    installed = BotSkill(bot_id=bot.id, skill_id=skill.id, status="installed")
    installed.installed_at = utcnow()
    db_session.add(installed)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    refused = await client.delete(path, headers={"If-Match": "1"})
    assert refused.status_code == 409 and bot.name in refused.json()["message"]

    # 停用后若还有待审申请，同样不能删。
    installed.status = "uninstalled"
    approval = SkillApproval(
        bot_id=bot.id,
        skill_id=skill.id,
        requested_security_prompt="",
        skill_revision=1,
        request_inputs_enc="{}",
        requested_by=creator.id,
    )
    db_session.add(approval)
    await db_session.commit()
    assert (await client.delete(path, headers={"If-Match": "1"})).status_code == 409
    approval.status = "withdrawn"
    await db_session.commit()

    assert (await client.delete(path, headers={"If-Match": "0"})).status_code == 409
    deleted = await client.delete(path, headers={"If-Match": "1"})
    assert deleted.status_code == 200, deleted.text
    db_session.expire_all()
    stored = await db_session.get(Skill, skill_id)
    assert stored.deleted_at is not None and not stored.enabled and stored.revision == 2
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "skill.delete"))
    assert audit.target_id == str(skill_id) and audit.diff["name"] == ["query", None]

    assert (await client.get("/api/admin/skills")).json()["data"]["total"] == 0
    patch = await client.patch(path, json={"enabled": True}, headers={"If-Match": "2"})
    assert patch.status_code == 404
    assert (await client.delete(path, headers={"If-Match": "2"})).status_code == 404
    # 历史申请仍能看出是哪个技能。
    approvals = (await client.get("/api/admin/skill-approvals")).json()["data"]["items"]
    assert approvals[0]["skill_name"] == "query"

    await login_existing(client, db_session, await db_session.get(User, creator_id))
    listing = await client.get(f"/api/admin/bots/{bot_id}/skills")
    assert listing.json()["data"]["items"] == []
    install = await client.post(f"/api/admin/bots/{bot_id}/skills/{skill_id}/install", json={})
    assert install.status_code == 404


async def test_sync_skips_deleted_skill_and_same_name_create_restores_it(
    client, db_session, monkeypatch
):
    await login_as(client, db_session, role="ai_committee")
    source = (
        await client.post(
            "/api/admin/skill-sources",
            json={
                "key": "market",
                "label": "Market",
                "git_url": "https://github.com/example/s.git",
            },
        )
    ).json()["data"]
    monkeypatch.setattr(
        "coreman.api.routers.skill_catalog.fetch_catalog",
        lambda _url: [
            {"name": "keep-me", "description": "", "version": "1.0"},
            {"name": "drop-me", "description": "", "version": "1.0"},
        ],
    )
    sync = f"/api/admin/skill-sources/{source['id']}/sync"
    assert (await client.post(sync, headers={"If-Match": "1"})).json()["data"]["created"] == 2
    dropped = await db_session.scalar(select(Skill).where(Skill.name == "drop-me"))
    assert (
        await client.delete(f"/api/admin/skills/{dropped.id}", headers={"If-Match": "1"})
    ).status_code == 200

    again = await client.post(sync, headers={"If-Match": "1"})
    assert again.json()["data"] == {"created": 0, "updated": 0, "unchanged": 1, "skipped": 1}
    names = [row["name"] for row in (await client.get("/api/admin/skills")).json()["data"]["items"]]
    assert names == ["keep-me"]

    restored = await client.post(
        "/api/admin/skills",
        json={"name": "drop-me", "source_id": source["id"], "description": "back"},
    )
    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    assert data["id"] == str(dropped.id) and data["description"] == "back"
    assert data["enabled"] is False
    db_session.expire_all()
    assert (await db_session.get(Skill, uuid.UUID(data["id"]))).deleted_at is None
    assert await db_session.scalar(select(AuditLog).where(AuditLog.action == "skill.restore"))
    names = [row["name"] for row in (await client.get("/api/admin/skills")).json()["data"]["items"]]
    assert names == ["drop-me", "keep-me"]
