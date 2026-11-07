"""Anonymous read-only catalog MCP: stored catalogs of every enabled system, no tokens, no calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers.systems_catalog import PUBLIC_PATH
from coreman.core.db.models import BusinessSystem, BusinessTokenIssue
from coreman.core.systems_catalog import service
from tests.api.test_systems_catalog import bot_cipher, fake, turn  # noqa: F401
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem


@pytest.fixture
def public(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state.settings, "systems_catalog_public", True)


async def fetched(db_session: AsyncSession) -> None:
    """The stock catalog as a task last fetched it, plus systems the catalog must leave out."""
    _, member, _ = await turn(db_session)
    db_session.add_all(
        [
            BusinessSystem(
                key="off", name="停用", base_url=BASE_URL, openapi_url=SPEC_URL, enabled=False
            ),
            BusinessSystem(key="bare", name="无目录", base_url=BASE_URL),
        ]
    )
    await db_session.commit()
    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    await service.refresh(
        db_session,
        service.Issuance(bot_cipher(), "coreman", None, {}),
        system,
        service.Operator(member, "zhangsan"),
    )


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = await client.post(PUBLIC_PATH, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def call(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await rpc(client, "tools/call", {"name": name, "arguments": arguments})
    text = result["content"][0]["text"]
    assert "eyJ" not in text
    return json.loads(text)  # type: ignore[no-any-return]


async def test_off_by_default(client: httpx.AsyncClient) -> None:
    r = await client.post(PUBLIC_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 404
    assert (await client.get(PUBLIC_PATH)).status_code == 404


async def test_lists_every_enabled_catalog_without_tokens(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    public: None,
) -> None:
    await fetched(db_session)
    before = len(fake.requests)
    issues = await db_session.scalar(select(func.count()).select_from(BusinessTokenIssue))

    names = [tool["name"] for tool in (await rpc(client, "tools/list"))["tools"]]
    assert names == ["systems_browse", "systems_search", "systems_describe"]
    systems = await call(client, "systems_browse", {})
    # Only enabled systems with an OpenAPI URL; nothing is filtered by permission.
    assert [s["key"] for s in systems["systems"]] == ["stock"]
    stock = systems["systems"][0]
    assert stock["call_mode"] == "direct" and stock["operations"] == 6
    assert stock["permission_check"] == "not_checked" and "permissions_unknown" not in stock
    modules = await call(client, "systems_browse", {"system": "stock"})
    assert "直连" in modules["call_rules"] and modules["permission_check"] == "not_checked"
    found = await call(client, "systems_search", {"query": "作废 单据"})
    assert found["items"][0]["operation"].startswith("cancelDocument")
    assert found["items"][0]["operation"].endswith("[destructive; 需确认]")
    detail = await call(
        client, "systems_describe", {"system": "stock", "operation_id": "cancelDocument"}
    )
    assert detail["url"] == BASE_URL + "/api/stock/documents/{id}/cancel"
    assert detail["call"]["mode"] == "direct" and detail["call"]["token_env"] == "SYSTEMS_TOKEN"
    assert '"Authorization: Bearer $SYSTEMS_TOKEN"' in detail["call"]["curl"]
    assert (await call(client, "systems_browse", {"system": "off"}))["error"] == (
        "system_not_available"
    )
    # Proxied calls are not offered here even when asked for by name.
    refused = await call(
        client, "systems_call", {"system": "stock", "operation_id": "listDocuments"}
    )
    assert refused["error"] == "invalid_tool_or_arguments"

    # The business system was never contacted and no token was issued.
    assert len(fake.requests) == before
    after = await db_session.scalar(select(func.count()).select_from(BusinessTokenIssue))
    assert after == issues


async def test_unfetched_catalog_is_reported_not_fetched(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    public: None,
) -> None:
    await turn(db_session)
    systems = await call(client, "systems_browse", {})
    assert systems["systems"][0]["catalog"] == "unavailable"
    assert (await call(client, "systems_browse", {"system": "stock"}))["error"] == (
        "catalog_unavailable"
    )
    assert fake.requests == []


async def test_token_env_names_the_callers_variable(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    public: None,
) -> None:
    await fetched(db_session)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "systems_describe",
            "arguments": {"system": "stock", "operation_id": "listDocuments"},
        },
    }
    r = await client.post(PUBLIC_PATH + "?token_env=MY_PAT", json=body)
    detail = json.loads(r.json()["result"]["content"][0]["text"])
    assert detail["call"]["token_env"] == "MY_PAT"
    assert '"Authorization: Bearer $MY_PAT"' in detail["call"]["curl"]
    assert "MY_PAT 是调用方本机的环境变量" in detail["call"]["notes"]
    for bad in ("1PAT", "A-B", "$(id)", "A" * 65):
        bad_r = await client.post(PUBLIC_PATH, params={"token_env": bad}, json=body)
        assert bad_r.status_code == 400


async def test_rejects_browsers_and_large_bodies(
    client: httpx.AsyncClient,
    public: None,
) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = await client.post(PUBLIC_PATH, json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "a" * 20_000}}
    assert (await client.post(PUBLIC_PATH, json=big)).status_code == 413
    assert (await client.get(PUBLIC_PATH)).status_code == 405
