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
    assert models[0] == "codex/gpt-5.5" and all(m.startswith("codex/") for m in models)
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
            "sort_order": 200,
            "is_default": True,
        },
    )
    assert r.status_code == 201, r.text
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
    assert [x["model"] for x in rows if x["is_default"]] == ["claude-sonnet-4-6"]
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
    # 同名模型在 claude 下未被使用，可删；minimax 下在用，409
    freed = await client.delete("/api/admin/model-catalog/claude/minimax%2FMiniMax-M2.7")
    assert freed.status_code == 200
    in_use = await client.delete("/api/admin/model-catalog/minimax/minimax%2FMiniMax-M2.7")
    assert in_use.status_code == 409


async def test_default_handoff_is_audited(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="ai_committee")
    r = await client.patch("/api/admin/model-catalog/codex/codex%2Fgpt-5.5", json={"retired": True})
    assert r.status_code == 200
    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "catalog.update").order_by(AuditLog.id.desc())
    )
    audit = rows.scalars().first()
    assert audit is not None and audit.diff is not None
    assert audit.diff["retired"] == [False, True]
    assert audit.diff["default_model"] == ["codex/gpt-5.5", "codex/gpt-5.4"]
