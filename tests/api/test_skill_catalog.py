from sqlalchemy import select

from coreman.core.db.models import AuditLog, EnvPreset, Skill
from coreman.core.knowledge.skill_policy import selected_groups
from tests.api.conftest import login_as


async def test_catalog_permissions_revision_and_secrets(client, db_session):
    await login_as(client, db_session, role="ai_committee")
    source = await client.post(
        "/api/admin/skill-sources",
        json={"key": "test", "label": "Test", "git_url": "https://github.com/example/skills.git"},
    )
    assert source.status_code == 200, source.text
    source_id = source.json()["data"]["id"]
    body = {
        "name": "test-mcp",
        "source_id": source_id,
        "install_type": "mcp",
        "security_level": "internal",
        "mcp_config": {
            "type": "http",
            "url": "https://tools.example/mcp",
            "headers": {"Authorization": "Bearer TOP-SECRET"},
        },
    }
    result = await client.post("/api/admin/skills", json=body)
    assert result.status_code == 200, result.text
    row = result.json()["data"]
    assert row["enabled"] is False and row["has_mcp_config"] is True
    assert "TOP-SECRET" not in result.text and "mcp_config" not in row
    stored = await db_session.scalar(select(Skill))
    assert "TOP-SECRET" not in stored.mcp_config_enc
    body.pop("mcp_config")
    result = await client.put(
        f"/api/admin/skills/{row['id']}", json=body | {"enabled": True}, headers={"If-Match": "1"}
    )
    assert result.status_code == 200 and result.json()["data"]["revision"] == 2
    assert (
        await client.put(f"/api/admin/skills/{row['id']}", json=body, headers={"If-Match": "1"})
    ).status_code == 409
    await login_as(client, db_session, role="member")
    assert (await client.get("/api/admin/skills")).json()["data"]["total"] == 1
    assert (await client.post("/api/admin/skills", json=body)).status_code == 403
    assert (await client.get("/api/admin/env-presets")).status_code == 403
    assert "TOP-SECRET" not in str([row.diff for row in await db_session.scalars(select(AuditLog))])


async def test_status_toggle_only_changes_enabled_and_is_audited(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    source = await client.post(
        "/api/admin/skill-sources",
        json={"key": "toggle", "label": "Toggle", "git_url": "https://github.com/example/s.git"},
    )
    body = {
        "name": "toggle-query",
        "source_id": source.json()["data"]["id"],
        "security_level": "internal",
        "env_groups": ["oss"],
        "security_prompt_template": "Read only",
    }
    row = (await client.post("/api/admin/skills", json=body)).json()["data"]
    path = f"/api/admin/skills/{row['id']}"
    result = await client.patch(path, json={"enabled": True}, headers={"If-Match": "1"})
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["enabled"] is True and data["revision"] == 2
    assert data["env_groups"] == ["oss"] and data["security_prompt_template"] == "Read only"
    # 旧修订号、多余字段都拒绝；状态没变时不递增修订号，也不记审计。
    assert (
        await client.patch(path, json={"enabled": False}, headers={"If-Match": "1"})
    ).status_code == 409
    assert (
        await client.patch(path, json={"enabled": False, "name": "x"}, headers={"If-Match": "2"})
    ).status_code == 422
    same = await client.patch(path, json={"enabled": True}, headers={"If-Match": "2"})
    assert same.json()["data"]["revision"] == 2
    off = await client.patch(path, json={"enabled": False}, headers={"If-Match": "2"})
    assert off.json()["data"]["enabled"] is False and off.json()["data"]["revision"] == 3
    actions = [
        (log.action, log.diff)
        for log in await db_session.scalars(
            select(AuditLog).where(AuditLog.target_id == row["id"]).order_by(AuditLog.id)
        )
    ]
    assert actions[1:] == [
        ("skill.enable", {"enabled": [False, True]}),
        ("skill.disable", {"enabled": [True, False]}),
    ]
    await login_as(client, db_session, role="member")
    assert (
        await client.patch(path, json={"enabled": True}, headers={"If-Match": "3"})
    ).status_code == 403


async def test_presets_forbid_identity_and_doris_fallback(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    body = {"group_key": "db_erp", "label": "ERP", "vars": {"DB_PASSWORD": "secret-erp"}}
    path = "/api/admin/env-presets/db_erp"
    result = await client.put(path, json=body, headers={"If-Match": "0"})
    assert result.status_code == 200, result.text
    assert "secret-erp" not in result.text
    mask = result.json()["data"]["vars"]
    assert (
        await client.put(path, json=body | {"vars": mask}, headers={"If-Match": "1"})
    ).status_code == 200
    for key in (
        "COREMAN_USER_LOGIN",
        "BOT_USER_LOGIN",
        "BOT_TOKEN_STAT",
        "COREMAN_SYSTEMS",
        "NODE_OPTIONS",
    ):
        assert (
            await client.put(path, json=body | {"vars": {key: "forged"}}, headers={"If-Match": "2"})
        ).status_code == 422
    stored = await db_session.scalar(select(EnvPreset))
    assert "secret-erp" not in stored.vars_enc
    skill = Skill(
        env_groups=[],
        selectable_env_groups={"db_erp": "ERP", "db_bifrost": "Bifrost"},
        data_sources={"doris": "Doris", "mysql": "MySQL"},
        default_data_source="doris",
        doris_enabled_groups=["db_erp"],
    )
    assert selected_groups(skill, ["db_erp", "db_bifrost"], None) == ["db_erp_doris", "db_bifrost"]
    assert selected_groups(skill, ["db_erp"], "mysql") == ["db_erp"]


async def test_source_sync_imports_disabled_entries_and_attributes_actor(
    client, db_session, monkeypatch
):
    actor = await login_as(client, db_session, role="ai_committee")
    source = (
        await client.post(
            "/api/admin/skill-sources",
            json={
                "key": "market",
                "label": "Marketplace",
                "git_url": "https://github.com/example/skills.git",
            },
        )
    ).json()["data"]
    monkeypatch.setattr(
        "coreman.api.routers.skill_catalog.fetch_catalog",
        lambda url: [
            {"name": "query-sync", "description": "Query", "version": "1.0"},
        ],
    )
    path = f"/api/admin/skill-sources/{source['id']}/sync"
    response = await client.post(path, headers={"If-Match": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"created": 1, "updated": 0, "unchanged": 0, "skipped": 0}
    stored = await db_session.scalar(select(Skill).where(Skill.name == "query-sync"))
    assert not stored.enabled
    assert str(stored.source_id) == source["id"]
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "skill.catalog_sync"))
    assert audit.actor_id == actor.id
    assert audit.actor_login == actor.login_name
    stored.enabled, stored.security_level = True, "internal"
    stored.security_prompt_template = "read-only"
    await db_session.commit()
    monkeypatch.setattr(
        "coreman.api.routers.skill_catalog.fetch_catalog",
        lambda url: [
            {"name": "query-sync", "description": "Updated", "version": "2.0"},
        ],
    )
    response = await client.post(path, headers={"If-Match": "1"})
    assert response.json()["data"]["updated"] == 1
    await db_session.refresh(stored)
    assert stored.enabled and stored.security_level == "internal"
    assert stored.security_prompt_template == "read-only"


async def test_source_sync_checks_role_revision_and_repository_before_fetch(
    client, db_session, monkeypatch
):
    from unittest.mock import Mock

    await login_as(client, db_session, role="platform_admin")
    source = (
        await client.post("/api/admin/skill-sources", json={"key": "manual", "label": "Manual"})
    ).json()["data"]
    fetch = Mock()
    monkeypatch.setattr("coreman.api.routers.skill_catalog.fetch_catalog", fetch)
    path = f"/api/admin/skill-sources/{source['id']}/sync"
    assert (await client.post(path, headers={"If-Match": "0"})).status_code == 409
    assert (await client.post(path, headers={"If-Match": "1"})).status_code == 422
    await login_as(client, db_session, role="member")
    assert (await client.post(path, headers={"If-Match": "1"})).status_code == 403
    fetch.assert_not_called()


async def test_source_sync_fetch_error_is_sanitized(client, db_session, monkeypatch):
    await login_as(client, db_session, role="platform_admin")
    source = (
        await client.post(
            "/api/admin/skill-sources",
            json={
                "key": "bad",
                "label": "Bad",
                "git_url": "https://github.com/example/skills.git",
            },
        )
    ).json()["data"]

    def fail(url):
        raise ValueError("SECRET-from-git-output")

    monkeypatch.setattr("coreman.api.routers.skill_catalog.fetch_catalog", fail)
    response = await client.post(
        f"/api/admin/skill-sources/{source['id']}/sync", headers={"If-Match": "1"}
    )
    assert response.status_code == 422
    assert "SECRET" not in response.text
    assert not list(await db_session.scalars(select(Skill)))


async def test_preset_rejects_edited_masks_without_overwriting_secret(client, db_session, app):
    await login_as(client, db_session, role="platform_admin")
    body = {"group_key": "mask_guard", "label": "Mask guard", "vars": {"PASSWORD": "abc"}}
    path = "/api/admin/env-presets/mask_guard"
    response = await client.put(path, json=body, headers={"If-Match": "0"})
    mask = response.json()["data"]["vars"]["PASSWORD"]
    assert mask == "••••"
    for damaged in ("•••", "•", "x••••", "••••x"):
        response = await client.put(
            path, json=body | {"vars": {"PASSWORD": damaged}}, headers={"If-Match": "1"}
        )
        assert response.status_code == 422
    stored = await db_session.scalar(select(EnvPreset).where(EnvPreset.group_key == "mask_guard"))
    assert stored.version == 1
    from coreman.core.bots.secrets import decrypt_json

    assert (
        decrypt_json(app.state.cipher, stored.vars_enc, "env_presets.vars_enc")["PASSWORD"] == "abc"
    )
    response = await client.put(
        path, json=body | {"vars": {"PASSWORD": mask}}, headers={"If-Match": "1"}
    )
    assert response.status_code == 200
    response = await client.put(
        path, json=body | {"vars": {"PASSWORD": "complete-new-value"}}, headers={"If-Match": "2"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["vars"]["PASSWORD"] == "co••••ue"


async def test_source_access_token_encrypted_preserved_scoped_and_removed(
    client, db_session, app, monkeypatch
):
    import uuid

    from coreman.core.db.models import SkillSource
    from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD

    actor = await login_as(client, db_session, role="platform_admin")
    token = "glpat-example-test-only"
    body = {
        "key": "token_source",
        "label": "Token source",
        "git_url": "git@git.example.com:group/repo.git",
        "access_token": token,
    }
    response = await client.post("/api/admin/skill-sources", json=body)
    assert response.status_code == 200, response.text
    row = response.json()["data"]
    assert row["has_access_token"] is True
    assert token not in response.text and "access_token_enc" not in response.text
    stored = await db_session.get(SkillSource, uuid.UUID(row["id"]))
    assert token not in stored.access_token_enc
    assert app.state.cipher.decrypt(stored.access_token_enc, SOURCE_TOKEN_AAD) == token
    path = f"/api/admin/skill-sources/{row['id']}"
    body["access_token"] = ""
    response = await client.put(path, json=body, headers={"If-Match": "1"})
    assert response.status_code == 200 and response.json()["data"]["has_access_token"]
    seen = []
    monkeypatch.setattr(
        "coreman.api.routers.skill_catalog.fetch_catalog",
        lambda url, value: seen.append((url, value)) or [],
    )
    response = await client.post(path + "/sync", headers={"If-Match": "2"})
    assert response.status_code == 200
    assert seen == [(body["git_url"], token)]
    response = await client.put(
        path, json=body | {"git_url": "https://other.example/other.git"}, headers={"If-Match": "2"}
    )
    assert response.status_code == 422
    response = await client.put(
        path, json=body | {"remove_access_token": True}, headers={"If-Match": "2"}
    )
    assert response.status_code == 200 and not response.json()["data"]["has_access_token"]
    await db_session.refresh(stored)
    assert stored.access_token_enc is None
    logs = list(await db_session.scalars(select(AuditLog).where(AuditLog.actor_id == actor.id)))
    assert token not in str([log.diff for log in logs])


async def test_source_token_rejects_invalid_secret_without_echoing_it(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    token = "glpat-secret\ninjected"
    response = await client.post(
        "/api/admin/skill-sources",
        json={
            "key": "bad_token",
            "label": "Bad",
            "git_url": "https://git.example.com/repo.git",
            "access_token": token,
        },
    )
    assert response.status_code == 422
    assert "glpat-secret" not in response.text
