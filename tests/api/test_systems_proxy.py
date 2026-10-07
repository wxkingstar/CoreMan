"""Phase 2 end to end: a proxied system from prompt to systems_describe to systems_call."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import ENV_RISK_RULES, PROXY_RULES, build_system_access
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AuditLog,
    BotSystemGrant,
    BusinessSystem,
    BusinessTokenIssue,
    SystemCall,
    SystemCatalog,
    Task,
    User,
)
from coreman.core.prompting.system_prompt import Speaker
from coreman.core.systems_catalog import policy
from coreman.core.systems_catalog.compiler import COMPILER_VERSION
from tests.api.conftest import login_as
from tests.api.test_bots import _bot_body
from tests.api.test_systems_catalog import URL, bearer, call
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.worker_helpers import MASTER, seed_bot


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    install(monkeypatch, system)
    return system


async def proxied_turn(
    db_session: AsyncSession, *, delivery: str = "proxy", write: bool = False
) -> tuple[Any, User, Task]:
    bot, _, _ = await seed_bot(db_session)
    member = User(login_name="zhangsan", display_name="张三", email="zhangsan@example.test")
    db_session.add_all(
        [
            member,
            BusinessSystem(
                key="stock",
                name="库存",
                base_url=BASE_URL,
                openapi_url=SPEC_URL,
                token_delivery=delivery,
                allowed_bot_ids=None,
            ),
        ]
    )
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="stock", allow_write=write))
    task = Task(bot_id=bot.id, kind="chat", payload={}, status="running")
    db_session.add(task)
    await db_session.commit()
    return bot, member, task


def tokens_seen(fake: FakeBusinessSystem, path: str) -> list[str | None]:
    return [fake.token(r) for r in fake.requests if r.url.path == path]


async def test_proxied_system_gets_no_runtime_token(
    db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    bot, member, task = await proxied_turn(db_session)
    speaker = Speaker("zs", member.id, member.login_name, member.display_name)
    mount = policy.Mount(task.id, "https://coreman.example.com" + URL, 1800)
    access = await build_system_access(
        db_session,
        Cipher(MASTER),
        bot=bot,
        speaker=speaker,
        issuer="coreman",
        external_key=None,
        catalog=mount,
    )
    await db_session.commit()
    assert not [key for key in access.env if key.startswith("BOT_TOKEN_")]
    assert access.issued == []
    assert "- 库存 (stock)：https://stock.example.com（平台代理：用 systems_call 调用）" in (
        access.prompt
    )
    assert access.prompt.endswith(PROXY_RULES)
    assert "令牌只属于当前发言者" not in access.prompt and ENV_RISK_RULES not in access.prompt
    assert '"token_delivery": "proxy"' in access.env["COREMAN_SYSTEMS"]
    # Without a runtime that mounts the catalog, the system cannot be used at all.
    plain = await build_system_access(
        db_session, Cipher(MASTER), bot=bot, speaker=speaker, issuer="coreman", external_key=None
    )
    assert "本轮无法调用" in plain.prompt and policy.URL_ENV not in plain.env
    assert not (await db_session.execute(select(BusinessTokenIssue))).scalars().all()


async def test_read_write_and_risk_policy(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    bot, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    listed = await client.post(
        URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth
    )
    assert "systems_call" in [tool["name"] for tool in listed.json()["result"]["tools"]]

    detail = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "createDraft"}
    )
    assert detail["call"]["mode"] == "proxy" and detail["call"]["allowed"] is False
    read = await call(
        client,
        auth,
        "systems_call",
        {"system": "stock", "operation_id": "listDocuments", "query": {"status": "confirmed"}},
    )
    assert read["status"] == 200 and read["data"]["data"]["items"][0]["id"] == "D1"
    assert read["content_trust"] == "system_returned_data"

    write = {
        "system": "stock",
        "operation_id": "createDraft",
        "body": {"sku": "SKU-9", "quantity": 3},
    }
    denied = await call(client, auth, "systems_call", write)
    assert denied["error"] == "write_not_allowed"
    grant = await db_session.get(BotSystemGrant, (bot.id, "stock"))
    assert grant is not None
    grant.allow_write = True
    await db_session.commit()
    created = await call(client, auth, "systems_call", write)
    assert created["ok"] is True and created["data"]["data"]["sku"] == "SKU-9"

    for op_id, risk in (("cancelDocument", "destructive"), ("refundSupplier", "financial")):
        result = await call(
            client,
            auth,
            "systems_call",
            {"system": "stock", "operation_id": op_id, "path_params": {"id": "D1"}},
        )
        assert result["error"] == "risk_not_allowed" and result["risk"] == risk
    assert all(r.url.path != "/api/stock/documents/D1/cancel" for r in fake.requests)
    assert fake.documents["D1"]["status"] == "confirmed"

    # One token for the whole task, issued on first use and used for permissions and calls.
    issues = (
        (
            await db_session.execute(
                select(BusinessTokenIssue).where(BusinessTokenIssue.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    proxy_issues = [i for i in issues if i.purpose == "proxy"]
    assert len(proxy_issues) == 1
    used = set(tokens_seen(fake, "/api/stock/documents") + tokens_seen(fake, "/api/stock/me"))
    assert len(used) == 1
    calls = (await db_session.execute(select(SystemCall).order_by(SystemCall.id))).scalars().all()
    assert [(c.operation_id, c.outcome, c.status_code) for c in calls] == [
        ("listDocuments", "ok", 200),
        ("createDraft", "denied", None),
        ("createDraft", "ok", 200),
        ("cancelDocument", "denied", None),
        ("refundSupplier", "denied", None),
    ]
    assert calls[0].token_id == proxy_issues[0].token_id and calls[0].duration_ms is not None
    await db_session.refresh(task)
    stored = task.payload["systems_catalog"]["tokens"]["stock"]
    # Only the encrypted value is kept with the task.
    assert set(stored) == {"enc", "expires_at", "auth_mode", "token_id"}
    assert next(iter(used)) not in str(task.payload)


async def test_arguments_are_validated_and_responses_bounded(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    invalid = await call(
        client,
        auth,
        "systems_call",
        {"system": "stock", "operation_id": "listDocuments", "query": {"status": "gone", "x": 1}},
    )
    assert invalid["error"] == "invalid_arguments" and len(invalid["invalid"]) == 2
    big = await call(
        client, auth, "systems_call", {"system": "stock", "operation_id": "dailyReport", "body": {}}
    )
    assert big["ok"] is True and big["truncated"] is True and "分页" in big["hint"]
    assert len(big["data"]["data"]) < 5000
    hidden = await call(
        client, auth, "systems_call", {"system": "stock", "operation_id": "rebuildIndex"}
    )
    assert hidden["error"] == "operation_not_found"
    assert not any(r.url.path == "/api/stock/internal/rebuild" for r in fake.requests)


async def test_echoed_token_is_redacted(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    spec = fake.spec
    assert isinstance(spec, dict)
    spec["paths"]["/api/stock/echo"] = {
        "get": {
            "operationId": "echo",
            "tags": ["documents"],
            "summary": "回显请求头",
            "x-permission": "none",
            "responses": {"200": {"description": "OK"}},
        }
    }
    fake.routes[("GET", "/api/stock/echo")] = lambda request: httpx.Response(
        200, json={"cookie": request.headers.get("cookie"), "token": fake.token(request)}
    )
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    result = await call(client, auth, "systems_call", {"system": "stock", "operation_id": "echo"})
    assert result["data"] == {"cookie": "bot_token=[REDACTED]", "token": "[REDACTED]"}


async def test_env_systems_keep_curl_and_refuse_proxying(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await proxied_turn(db_session, delivery="env")
    auth = bearer(app, task, member)
    listed = await client.post(
        URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth
    )
    assert "systems_call" not in [tool["name"] for tool in listed.json()["result"]["tools"]]
    result = await call(client, auth, "systems_call", {"system": "stock", "operation_id": "getMe"})
    assert result["error"] == "not_proxied"
    assert not await db_session.scalar(select(SystemCall.id))


async def test_outdated_catalog_is_recompiled_in_full(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    await call(client, auth, "systems_browse", {"system": "stock"})
    row = await db_session.get(SystemCatalog, "stock", populate_existing=True)
    assert row is not None and row.compiler_version == COMPILER_VERSION
    row.compiler_version = COMPILER_VERSION - 1
    row.checked_at = datetime.now(UTC) - timedelta(minutes=2)
    await db_session.commit()
    await call(client, auth, "systems_search", {"query": "单据"})
    specs = [r for r in fake.requests if r.url.path == "/openapi.yaml"]
    assert len(specs) == 2 and "if-none-match" not in specs[1].headers
    await db_session.refresh(row)
    assert row.compiler_version == COMPILER_VERSION


async def test_admin_switches_delivery_and_write_grants(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    owner = await login_as(client, db_session, role="platform_admin")
    bot = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    body = {
        "key": "stock",
        "name": "库存",
        "base_url": BASE_URL,
        "openapi_url": SPEC_URL,
        "allowed_bot_ids": None,
    }
    system = (await client.post("/api/admin/systems", json=body)).json()["data"]
    assert system["token_delivery"] == "env"
    r = await client.put(
        "/api/admin/systems/stock",
        json={**body, "token_delivery": "proxy"},
        headers={"If-Match": str(system["version"])},
    )
    assert r.status_code == 200 and r.json()["data"]["token_delivery"] == "proxy"
    # Omitting the field keeps the stored choice.
    r = await client.put(
        "/api/admin/systems/stock",
        json={**body, "name": "库存系统"},
        headers={"If-Match": str(r.json()["data"]["version"])},
    )
    assert r.json()["data"]["token_delivery"] == "proxy"
    diffs = (
        await db_session.execute(select(AuditLog.diff).where(AuditLog.action == "system.update"))
    ).scalars()
    assert any(d and "token_delivery" in d for d in diffs)

    grants = f"/api/admin/bots/{bot['id']}/system-grants"
    current = (await client.get(grants)).json()["data"]
    bad = await client.put(
        grants,
        json={"system_keys": [], "write_keys": ["stock"]},
        headers={"If-Match": str(current["version"])},
    )
    assert bad.status_code == 422
    r = await client.put(
        grants,
        json={"system_keys": ["stock"], "write_keys": ["stock"]},
        headers={"If-Match": str(current["version"])},
    )
    assert r.json()["data"] == {
        "system_keys": ["stock"],
        "write_keys": ["stock"],
        "version": current["version"] + 1,
    }
    # Old clients that do not send write_keys keep the write permission of retained systems.
    r = await client.put(
        grants,
        json={"system_keys": ["stock"]},
        headers={"If-Match": str(r.json()["data"]["version"])},
    )
    assert r.json()["data"]["write_keys"] == ["stock"]
    assert (await client.get(grants)).json()["data"]["write_keys"] == ["stock"]
    assert owner.id


async def test_catalog_from_an_old_system_url_is_not_used(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await proxied_turn(db_session)
    auth = bearer(app, task, member)
    await call(client, auth, "systems_browse", {"system": "stock"})
    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    system.base_url = "https://stock-new.example.com"
    await db_session.commit()
    before = len(fake.requests)
    result = await call(client, auth, "systems_call", {"system": "stock", "operation_id": "getMe"})
    assert result["error"] == "catalog_unavailable"
    assert len(fake.requests) == before
