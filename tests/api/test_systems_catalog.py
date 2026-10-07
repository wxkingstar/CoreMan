"""Operation catalog end to end against a fake business system built from the contract example.

Admin save → fetch and compile → L0 prompt and MCP credentials for a turn → layered tools with
permission filtering, budgets and the task-bound capability.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import CATALOG_RULES, ENV_RISK_RULES, build_system_access
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AuditLog,
    BusinessSystem,
    BusinessTokenIssue,
    RuntimeNode,
    SystemCatalog,
    Task,
    User,
)
from coreman.core.prompting.system_prompt import Speaker
from coreman.core.systems_catalog import policy, service, tools
from tests.api.conftest import login_as
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.worker_helpers import MASTER, seed_bot

URL = policy.API_PATH


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem(permissions=["stock:doc:read"])
    install(monkeypatch, system)
    return system


async def turn(
    db_session: AsyncSession, *, openapi_url: str | None = SPEC_URL, granted: bool = True
) -> tuple[Any, User, Task]:
    bot, _, _ = await seed_bot(db_session)
    member = User(login_name="zhangsan", display_name="张三", email="zhangsan@example.test")
    db_session.add(member)
    db_session.add(
        BusinessSystem(
            key="stock",
            name="库存",
            base_url=BASE_URL,
            openapi_url=openapi_url,
            default_for_all_bots=granted,
            allowed_bot_ids=None,
        )
    )
    await db_session.flush()
    task = Task(bot_id=bot.id, kind="chat", payload={}, status="running")
    db_session.add(task)
    await db_session.commit()
    return bot, member, task


def bearer(app: Any, task: Task, member: User, ttl: int = 1800) -> dict[str, str]:
    token = policy.issue_capability(
        app.state.cipher, task_id=task.id, user_id=member.id, ttl_seconds=ttl
    )
    return {"Authorization": f"Bearer {token}"}


async def call(
    client: httpx.AsyncClient, auth: dict[str, str], name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = await client.post(URL, json=body, headers=auth)
    assert response.status_code == 200, response.text
    text = response.json()["result"]["content"][0]["text"]
    # No business token, capability or JWT ever comes back to the model.
    assert "eyJ" not in text and auth["Authorization"][7:] not in text
    return json.loads(text)  # type: ignore[no-any-return]


# Admin ------------------------------------------------------------------------------------------


async def test_admin_save_fetches_and_reports_the_catalog(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    await login_as(client, db_session, role="platform_admin")
    body = {"key": "stock", "name": "库存", "base_url": BASE_URL, "allowed_bot_ids": None}
    bad = await client.post(
        "/api/admin/systems", json={**body, "openapi_url": "https://evil.example/openapi.yaml"}
    )
    assert bad.status_code == 422
    # The old field name is still accepted and returned for one version.
    r = await client.post("/api/admin/systems", json={**body, "sitemap_url": SPEC_URL})
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["openapi_url"] == data["sitemap_url"] == SPEC_URL
    assert data["token_delivery"] == "env"
    catalog = data["catalog"]
    assert catalog["status"] == "ok" and catalog["error"] is None
    assert (catalog["module_count"], catalog["operation_count"], catalog["hidden_count"]) == (
        2,
        6,
        1,
    )
    assert catalog["lint_errors"] == 0
    row = await db_session.get(BusinessSystem, "stock")
    assert row is not None and row.sitemap_url == SPEC_URL
    issue = (await db_session.execute(select(BusinessTokenIssue))).scalar_one()
    assert issue.purpose == "catalog" and issue.task_id is None
    got = await client.get("/api/admin/systems/stock/catalog")
    assert got.json()["data"]["spec_url"] == SPEC_URL
    fake.spec = "openapi: 3.0.3\npaths: {}\n"
    r = await client.post("/api/admin/systems/stock/catalog/refresh")
    refreshed = r.json()["data"]
    assert refreshed["status"] == "ok" and refreshed["operation_count"] == 0
    assert refreshed["lint_errors"] >= 1 and refreshed["lint"][0]["rule"] == "oas3-schema"
    actions = set((await db_session.execute(select(AuditLog.action))).scalars())
    assert "system.catalog_refresh" in actions
    # Saving without touching the URLs does not fetch again.
    before = len(fake.requests)
    r = await client.put(
        "/api/admin/systems/stock",
        json={**body, "openapi_url": SPEC_URL, "name": "库存系统"},
        headers={"If-Match": str(data["version"])},
    )
    assert r.status_code == 200 and r.json()["data"]["catalog"] is None
    assert len(fake.requests) == before


async def test_refresh_needs_an_operator_identity(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    admin = await login_as(client, db_session, role="platform_admin")
    admin.email = None
    db_session.add(
        BusinessSystem(key="stock", name="库存", base_url=BASE_URL, openapi_url=SPEC_URL)
    )
    await db_session.commit()
    r = await client.post("/api/admin/systems/stock/catalog/refresh")
    assert r.status_code == 422
    assert fake.requests == []


# Prompt -----------------------------------------------------------------------------------------


async def test_turn_gets_catalog_lines_and_mcp_credentials(
    db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    bot, member, task = await turn(db_session)
    speaker = Speaker("zs", member.id, member.login_name, member.display_name)
    mount = policy.Mount(task.id, "https://coreman.example.com" + URL, 1800)

    async def access(catalog: policy.Mount | None):  # type: ignore[no-untyped-def]
        result = await build_system_access(
            db_session,
            bot_cipher(),
            bot=bot,
            speaker=speaker,
            issuer="coreman",
            external_key=None,
            catalog=catalog,
        )
        await db_session.commit()
        return result

    # Not fetched yet: the old one-line form, but the tools are mounted to fetch on demand.
    first = await access(mount)
    assert "- 库存: https://stock.example.com (env: BOT_TOKEN_STOCK; Cookie: bot_token)" in (
        first.prompt
    )
    assert CATALOG_RULES not in first.prompt
    assert ENV_RISK_RULES in first.prompt
    assert first.env[policy.URL_ENV] == "https://coreman.example.com" + URL
    cap = policy.read_capability(bot_cipher(), first.env[policy.TOKEN_ENV])
    assert (cap.task_id, cap.actor) == (task.id, member.id)
    config = json.loads(first.env["COREMAN_SYSTEMS"])[0]
    assert config["openapi_url"] == config["sitemap_url"] == SPEC_URL

    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    await service.refresh(
        db_session,
        service.Issuance(bot_cipher(), "coreman", None, {}),
        system,
        service.Operator(member, "zhangsan"),
    )
    second = await access(mount)
    assert (
        "- 库存 (stock)：https://stock.example.com，2 个模块 / 6 个操作"
        "（env: BOT_TOKEN_STOCK；Cookie: bot_token）"
    ) in second.prompt
    assert second.prompt.endswith(CATALOG_RULES)
    # A runtime that cannot mount the tools keeps the old prompt and gets no credentials.
    third = await access(None)
    assert CATALOG_RULES not in third.prompt and policy.URL_ENV not in third.env


async def test_mount_only_for_runtimes_that_declare_it(db_session: AsyncSession) -> None:
    bot, relay, _ = await seed_bot(db_session)
    kwargs = {
        "relay": relay,
        "model": bot.model,
        "task_id": 1,
        "public_base_url": "https://coreman.example.com/",
        "ttl_seconds": 1800,
    }
    assert await policy.mount_for(db_session, **kwargs) is None
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities = {"claude": {policy.RUNTIME_CAPABILITY: True}}
    await db_session.commit()
    mount = await policy.mount_for(db_session, **kwargs)
    assert mount == policy.Mount(1, "https://coreman.example.com" + URL, 1800)


def bot_cipher() -> Cipher:
    return Cipher(MASTER)


# MCP --------------------------------------------------------------------------------------------


async def test_layered_tools_with_permission_filter(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    init = await client.post(
        URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=auth,
    )
    assert init.json()["result"]["serverInfo"]["name"] == "coreman-systems"
    listed = await client.post(
        URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth
    )
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert names == ["systems_browse", "systems_search", "systems_describe"]

    systems = await call(client, auth, "systems_browse", {})
    assert systems["content_trust"] == "system_declared"
    # The member holds only stock:doc:read: getMe (none) and listDocuments are visible.
    assert systems["systems"] == [
        {
            "key": "stock",
            "name": "库存",
            "description": "",
            "base_url": BASE_URL,
            "catalog": "ok",
            "modules": 1,
            "operations": 2,
        }
    ]
    modules = await call(client, auth, "systems_browse", {"system": "stock"})
    assert modules["guide"].startswith("时间为 UTC")
    assert modules["modules"] == [
        {
            "name": "documents",
            "description": "出入库单据：创建草稿、确认、作废、查询",
            "operations": 2,
        }
    ]
    ops = await call(client, auth, "systems_browse", {"system": "stock", "module": "documents"})
    assert ops["operations"] == [
        "getMe  GET /api/stock/me  当前调用者身份与权限码",
        "listDocuments  GET /api/stock/documents  按状态分页查询出入库单据",
    ]
    found = await call(client, auth, "systems_search", {"query": "作废单据"})
    assert [item["operation"].split()[0] for item in found["items"]] == ["listDocuments", "getMe"]
    nothing = await call(client, auth, "systems_search", {"query": "退款"})
    assert nothing["items"] == [] and "message" in nothing
    hidden = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "cancelDocument"}
    )
    missing = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "noSuchThing"}
    )
    # Invisible and missing look the same, and nothing similar is offered instead.
    assert hidden["error"] == missing["error"] == "operation_not_found"
    assert hidden["message"] == missing["message"]
    detail = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "listDocuments"}
    )
    assert detail["url"] == BASE_URL + "/api/stock/documents"
    assert detail["parameters"][0]["enum"] == ["draft", "confirmed", "cancelled"]
    assert detail["call"]["mode"] == "env" and detail["call"]["token_env"] == "BOT_TOKEN_STOCK"
    assert '"Cookie: bot_token=$BOT_TOKEN_STOCK"' in detail["call"]["curl"]
    assert detail["content_trust"] == "system_declared"
    # One permission lookup for the whole task, with a short token recorded for this task.
    me = [r for r in fake.requests if r.url.path == "/api/stock/me"]
    assert len(me) == 1
    purposes = set(
        (
            await db_session.execute(
                select(BusinessTokenIssue.purpose).where(BusinessTokenIssue.task_id == task.id)
            )
        ).scalars()
    )
    assert purposes == {"catalog"}


async def test_wildcards_unknown_permissions_and_search(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    fake.permissions = ["stock:*"]
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    found = await call(client, auth, "systems_search", {"query": "作废 单据", "system": "stock"})
    assert found["items"][0] == {
        "system": "stock",
        "operation": "cancelDocument  POST /api/stock/documents/{id}/cancel  "
        "作废一张已确认的单据并回滚库存  [destructive]",
    }
    reads = await call(client, auth, "systems_search", {"query": "统计 出入库", "risk": "read"})
    assert reads["items"][0]["operation"].startswith("dailyReport  POST /api/stock/reports/daily")
    detail = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "createDraft"}
    )
    assert detail["risk"] == "write" and detail["body"]["schema"]["required"] == ["sku", "quantity"]
    assert "-d '<请求体>'" in detail["call"]["curl"]

    # A failing lookup does not filter, and says so.
    fake.permissions = None
    _, other, task2 = await turn_again(db_session)
    auth2 = bearer(app, task2, other)
    systems = await call(client, auth2, "systems_browse", {})
    assert systems["systems"][0]["permissions_unknown"] is True
    assert systems["systems"][0]["operations"] == 6


async def turn_again(db_session: AsyncSession) -> tuple[Any, User, Task]:
    task = (await db_session.execute(select(Task))).scalars().first()
    assert task is not None
    member = User(login_name="lisi", display_name="李四", email="lisi@example.test")
    second = Task(bot_id=task.bot_id, kind="chat", payload={}, status="running")
    db_session.add_all([member, second])
    await db_session.commit()
    return None, member, second


async def test_budget_and_repeats(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    args = {"system": "stock"}
    assert "error" not in await call(client, auth, "systems_browse", args)
    assert "error" not in await call(client, auth, "systems_browse", args)
    repeated = await call(client, auth, "systems_browse", args)
    assert repeated["error"] == "repeated_call" and repeated["stop"] is True
    for n in range(tools.MAX_CALLS - 3):
        await call(client, auth, "systems_search", {"query": f"q{n}"})
    over = await call(client, auth, "systems_search", {"query": "last"})
    assert over["error"] == "catalog_budget_exhausted" and over["stop"] is True
    await db_session.refresh(task)
    # The budget only stops the catalog tools; the turn itself keeps running.
    assert task.cancel_requested_at is None
    assert task.payload["systems_catalog"]["calls"] == tools.MAX_CALLS + 1


async def test_capability_is_bound_to_a_live_task(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert (await client.post(URL, json=ping)).status_code == 401
    assert (
        await client.post(URL, json=ping, headers={"Authorization": "Bearer not-a-capability"})
    ).status_code == 401
    expired = bearer(app, task, member, ttl=-1)
    assert (await client.post(URL, json=ping, headers=expired)).status_code == 401
    auth = bearer(app, task, member)
    assert (
        await client.post(URL, json=ping, headers={**auth, "Origin": "https://evil.example"})
    ).status_code == 403
    assert (await client.post(URL, json=ping, headers=auth)).status_code == 200
    assert (await client.get(URL, headers=auth)).status_code == 405
    # Another task's capability does not open this one, and a finished task closes its own.
    stranger = bearer(app, Task(id=task.id + 1000), member)
    assert (await client.post(URL, json=ping, headers=stranger)).status_code == 403
    task.status = "succeeded"
    await db_session.commit()
    assert (await client.post(URL, json=ping, headers=auth)).status_code == 403


async def test_tools_cannot_reach_systems_outside_the_grants(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session, granted=False)
    auth = bearer(app, task, member)
    assert (await call(client, auth, "systems_browse", {}))["systems"] == []
    result = await call(client, auth, "systems_describe", {"system": "stock", "operation_id": "x"})
    assert result["error"] == "system_not_available"
    assert fake.requests == []


async def test_recheck_uses_etag_and_keeps_a_stale_catalog(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    await call(client, auth, "systems_browse", {"system": "stock"})
    specs = [r for r in fake.requests if r.url.path == "/openapi.yaml"]
    assert len(specs) == 1
    # Within 10 minutes: no recheck.
    await call(client, auth, "systems_browse", {"system": "stock", "module": "documents"})
    assert len([r for r in fake.requests if r.url.path == "/openapi.yaml"]) == 1
    row = await db_session.get(SystemCatalog, "stock", populate_existing=True)
    assert row is not None
    row.checked_at = datetime.now(UTC) - timedelta(minutes=11)
    await db_session.commit()
    await call(client, auth, "systems_search", {"query": "单据"})
    specs = [r for r in fake.requests if r.url.path == "/openapi.yaml"]
    assert len(specs) == 2 and specs[1].headers.get("if-none-match")
    await db_session.refresh(row)
    assert row.status == "ok"
    row.checked_at = datetime.now(UTC) - timedelta(minutes=11)
    await db_session.commit()
    fake.spec_override = httpx.Response(503)
    result = await call(client, auth, "systems_search", {"query": "权限码"})
    assert result["items"][0]["operation"].startswith("getMe")
    await db_session.refresh(row)
    assert (row.status, row.error) == ("stale", "http_503")


async def test_broken_description_reports_error_without_a_catalog(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    lines = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    for index in range(1, 9):
        name, prev = chr(97 + index), chr(96 + index)
        lines.append(f"{name}: &{name} [" + ", ".join([f"*{prev}"] * 10) + "]")
    fake.spec = "\n".join(lines) + "\n"
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    result = await call(client, auth, "systems_browse", {"system": "stock"})
    assert result["error"] == "catalog_unavailable"
    row = await db_session.get(SystemCatalog, "stock")
    assert row is not None and row.status == "error" and row.compiled is None


async def test_unknown_tool_and_bad_arguments(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    auth = bearer(app, task, member)
    assert (await call(client, auth, "systems_call", {}))["error"] == "invalid_tool_or_arguments"
    bad = await call(client, auth, "systems_describe", {"system": "stock"})
    assert bad["error"] == "invalid_tool_or_arguments" and bad["invalid"]


async def test_permission_lookup_never_follows_an_old_system_url(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, task = await turn(db_session)
    await call(client, bearer(app, task, member), "systems_browse", {"system": "stock"})
    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    # The URL changed but the catalog was not fetched again yet (still within 10 minutes).
    system.base_url = "https://stock-new.example.com"
    await db_session.commit()
    _, other, second = await turn_again(db_session)
    before = len(fake.requests)
    result = await call(client, bearer(app, second, other), "systems_browse", {"system": "stock"})
    assert result["permissions_unknown"] is True
    assert len(fake.requests) == before
