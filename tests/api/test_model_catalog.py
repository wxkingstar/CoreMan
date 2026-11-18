import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog
from tests.api.conftest import login_as


async def test_list_seeded_for_member(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="member")
    r = await client.get("/api/admin/model-catalog", params={"provider": "codex"})
    assert r.status_code == 200
    models = [m["model"] for m in r.json()["data"]]
    assert models[0] == "codex/gpt-6.1-sol" and all(m.startswith("codex/") for m in models)
    assert r.json()["data"][0]["backend"] == "codex"
    created = await client.post(
        "/api/admin/model-catalog", json={"provider": "claude", "model": "x/y"}
    )
    assert created.status_code == 403


async def test_create_patch_default_and_delete(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="ai_committee")
    r = await client.post(
        "/api/admin/model-catalog",
        json={
            "provider": "claude",
            "model": "vllm/claude-new",
            "display_name": "New",
            "supports_xhigh": True,
            "supports_max": True,
            "sort_order": 200,
            "is_default": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["supports_max"] is True
    r = await client.patch(
        "/api/admin/model-catalog/claude/vllm%2Fclaude-new", json={"supports_max": False}
    )
    assert r.status_code == 200 and r.json()["data"]["supports_max"] is False
    listed = await client.get("/api/admin/model-catalog", params={"provider": "claude"})
    rows = listed.json()["data"]
    assert rows[0]["model"] == "vllm/claude-new" and [x for x in rows if x["is_default"]] == [
        rows[0]
    ]
    duplicated = await client.post(
        "/api/admin/model-catalog", json={"provider": "claude", "model": "vllm/claude-new"}
    )
    assert duplicated.status_code == 409
    r = await client.patch(
        "/api/admin/model-catalog/claude/vllm%2Fclaude-new", json={"retired": True}
    )
    assert r.status_code == 200
    assert r.json()["data"]["retired"] is True and r.json()["data"]["is_default"] is False
    listed = await client.get("/api/admin/model-catalog", params={"provider": "claude"})
    rows = listed.json()["data"]
    assert [x["model"] for x in rows if x["is_default"]] == ["claude-opus-5-5"]
    deleted = await client.delete("/api/admin/model-catalog/claude/vllm%2Fclaude-new")
    assert deleted.status_code == 200
    missing = await client.patch("/api/admin/model-catalog/claude/nope", json={"retired": True})
    assert missing.status_code == 404


async def test_retired_cannot_be_default(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(
        "/api/admin/model-catalog",
        json={
            "provider": "claude",
            "model": "vllm/claude-dead",
            "retired": True,
            "is_default": True,
        },
    )
    assert r.status_code == 422
    created = await client.post(
        "/api/admin/model-catalog",
        json={"provider": "claude", "model": "vllm/claude-dead", "retired": True},
    )
    assert created.status_code == 201
    rejected = await client.patch(
        "/api/admin/model-catalog/claude/vllm%2Fclaude-dead", json={"is_default": True}
    )
    assert rejected.status_code == 422
    revived = await client.patch(
        "/api/admin/model-catalog/claude/vllm%2Fclaude-dead",
        json={"is_default": True, "retired": False},
    )
    assert revived.status_code == 200


async def test_delete_in_use_is_provider_scoped(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import Bot, RelayServer

    admin = await login_as(client, db_session, role="platform_admin")
    relay = RelayServer(name="mm", host="h", clawrelay_port=1, model_provider="minimax")
    db_session.add(relay)
    await db_session.flush()
    db_session.add(
        Bot(
            bot_key="b1",
            platform="wecom",
            name="b",
            created_by=admin.id,
            relay_server_id=relay.id,
            model="minimax/MiniMax-M2.7",
            working_dir="/d",
            credentials_enc="enc",
        )
    )
    await db_session.commit()
    # 非默认目录模型仍可由管理员添加；删除的使用检查按 provider 隔离。
    for provider in ("claude", "minimax"):
        created = await client.post(
            "/api/admin/model-catalog",
            json={"provider": provider, "model": "minimax/MiniMax-M2.7"},
        )
        assert created.status_code == 201
    # 同名模型在 claude 下未被使用，可删；minimax 下在用，409
    freed = await client.delete("/api/admin/model-catalog/claude/minimax%2FMiniMax-M2.7")
    assert freed.status_code == 200
    in_use = await client.delete("/api/admin/model-catalog/minimax/minimax%2FMiniMax-M2.7")
    assert in_use.status_code == 409


async def test_default_handoff_is_audited(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="ai_committee")
    r = await client.patch(
        "/api/admin/model-catalog/codex/codex%2Fgpt-6.1-sol", json={"retired": True}
    )
    assert r.status_code == 200
    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "catalog.update").order_by(AuditLog.id.desc())
    )
    audit = rows.scalars().first()
    assert audit is not None and audit.diff is not None
    assert audit.diff["retired"] == [False, True]
    assert audit.diff["default_model"] == ["codex/gpt-6.1-sol", "codex/gpt-6-astra"]


async def test_replace_moves_bots_relays_and_default(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import Bot, ModelCatalog, RelayServer

    admin = await login_as(client, db_session, role="ai_committee")
    relay = RelayServer(
        name="codex-r",
        host="h",
        clawrelay_port=1,
        model_provider="codex",
        supported_models_mode="restricted",
        supported_models=["codex/gpt-6-astra", "codex/gpt-6.1-sol"],
    )
    db_session.add(relay)
    await db_session.flush()
    for key, model, effort in (
        ("sol", "codex/gpt-6.1-sol", "max"),
        ("astra", "codex/gpt-6-astra", "high"),
    ):
        db_session.add(
            Bot(
                bot_key=key,
                platform="wecom",
                name=key,
                created_by=admin.id,
                relay_server_id=relay.id,
                model=model,
                effort_level=effort,
                working_dir="/d",
                credentials_enc="enc",
            )
        )
    await db_session.commit()
    body = {
        "provider": "codex",
        "new": "codex/gpt-6.2-sol",
        "old": ["codex/gpt-6.1-sol"],
        "display_name": "GPT-6.2 Sol (Codex)",
        "supports_max": False,
    }

    preview = await client.post("/api/admin/model-catalog/replace", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    plan = preview.json()["data"]
    assert plan["created"] is True
    assert plan["default_before"] == "codex/gpt-6.1-sol"
    assert plan["default_after"] == "codex/gpt-6.2-sol"
    assert [(b["name"], b["new_effort_level"]) for b in plan["bots"]] == [("sol", "xhigh")]
    assert plan["relays"][0]["after"] == ["codex/gpt-6-astra", "codex/gpt-6.2-sol"]
    db_session.expire_all()
    assert await db_session.get(ModelCatalog, ("codex", "codex/gpt-6.2-sol")) is None
    sol = (await db_session.execute(select(Bot).where(Bot.bot_key == "sol"))).scalar_one()
    assert sol.model == "codex/gpt-6.1-sol"

    applied = await client.post("/api/admin/model-catalog/replace", json=body)
    assert applied.status_code == 200, applied.text
    db_session.expire_all()
    new = await db_session.get(ModelCatalog, ("codex", "codex/gpt-6.2-sol"))
    old = await db_session.get(ModelCatalog, ("codex", "codex/gpt-6.1-sol"))
    assert new is not None and old is not None
    assert (new.is_default, new.retired, new.supports_xhigh, new.supports_max) == (
        True,
        False,
        True,
        False,
    )
    assert new.display_name == "GPT-6.2 Sol (Codex)" and new.sort_order == old.sort_order
    assert (old.is_default, old.retired) == (False, True)
    bots = {
        b.bot_key: (b.model, b.effort_level, b.version)
        for b in (await db_session.execute(select(Bot))).scalars()
    }
    assert bots == {
        "sol": ("codex/gpt-6.2-sol", "xhigh", 2),
        "astra": ("codex/gpt-6-astra", "high", 1),
    }
    await db_session.refresh(relay)
    assert relay.supported_models == ["codex/gpt-6-astra", "codex/gpt-6.2-sol"]
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "catalog.replace"))
    ).scalar_one()
    assert audit.diff is not None
    assert audit.diff["default_model"] == ["codex/gpt-6.1-sol", "codex/gpt-6.2-sol"]

    # 已在目录里的新模型：取消退役、沿用原有标记；不是默认的旧模型被替代时默认不变。
    again = await client.post(
        "/api/admin/model-catalog/replace",
        json={"provider": "codex", "new": "codex/gpt-6.1-sol", "old": ["codex/gpt-6-astra"]},
    )
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["created"] is False and data["default_after"] == "codex/gpt-6.2-sol"
    db_session.expire_all()
    revived = await db_session.get(ModelCatalog, ("codex", "codex/gpt-6.1-sol"))
    assert revived is not None and not revived.retired and not revived.is_default


async def test_replace_rejects_bad_requests(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    url = "/api/admin/model-catalog/replace"
    await login_as(client, db_session, role="member")
    body = {"provider": "codex", "new": "codex/x", "old": ["codex/gpt-6.1-sol"]}
    assert (await client.post(url, json=body)).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    missing = await client.post(url, json={**body, "old": ["codex/nope"]})
    assert missing.status_code == 404 and "codex/nope" in missing.text
    same = await client.post(url, json={**body, "new": "codex/gpt-6.1-sol"})
    assert same.status_code == 422
