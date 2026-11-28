"""A member's own agent on the catalog MCP: filtered by the member's permissions, proxied calls
as the member, writes confirmed on a Feishu card from the public bot."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.api.routers import systems_catalog as router
from coreman.api.routers.systems_catalog import PUBLIC_PATH
from coreman.core.db.models import (
    BusinessSystem,
    BusinessTokenIssue,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Setting,
    SystemCall,
    Task,
    User,
    UserIdentity,
)
from coreman.core.db.models.oauth import OAuthClient, OAuthGrant
from coreman.core.db.session import make_session_factory
from coreman.core.oauth import server as oauth
from coreman.core.systems_catalog import member_writes, service
from coreman.core.timeutils import utcnow
from coreman.core.work_tasks.cards import NOTIFIER_SETTING
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.reply_buttons import admit
from coreman.runtime.worker import member_write_cards
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.api.test_systems_catalog import bot_cipher, turn
from tests.fakes.business_system import FakeBusinessSystem, install
from tests.integration.worker_helpers import build_ctx


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem(permissions=["stock:doc:read", "stock:doc:write"])
    install(monkeypatch, system)
    return system


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    from coreman.core.systems_catalog import context

    monkeypatch.setattr(context, "_own_tokens", {})
    monkeypatch.setattr(context, "_own_perms", {})
    monkeypatch.setattr(router, "_member_limit", router._RateLimit(120))
    monkeypatch.setattr(member_writes, "WAIT_SECONDS", 0.0)


async def setup(
    db_session: AsyncSession, *, delivery: str = "proxy", public_bot: bool = True
) -> tuple[Any, User, OAuthGrant, str]:
    """The stock catalog as a task last fetched it, a member with a Feishu identity and an
    OAuth access token with the `mcp` scope."""
    bot, member, _ = await turn(db_session)
    bot.platform = "feishu"
    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    system.token_delivery = delivery
    db_session.add(UserIdentity(user_id=member.id, platform="feishu", platform_user_id="ou_zs"))
    if public_bot:
        db_session.add(Setting(key=NOTIFIER_SETTING, value=str(bot.id)))
    client_id = f"cli-{uuid.uuid4().hex}"
    db_session.add(OAuthClient(client_id=client_id, client_name="Claude Code", redirect_uris=[]))
    await db_session.flush()
    grant = OAuthGrant(
        client_id=client_id,
        user_id=member.id,
        scope="mcp",
        refresh_hash=uuid.uuid4().hex,
        refresh_expires_at=utcnow() + timedelta(days=30),
    )
    db_session.add(grant)
    await db_session.flush()
    access = await oauth._mint_access(db_session, grant)
    await db_session.commit()
    await service.refresh(
        db_session,
        service.Issuance(bot_cipher(), "coreman", None, {}),
        system,
        service.Operator(member, "zhangsan"),
    )
    return bot, member, grant, access


def agent(app: Any, access: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {access}"},
    )


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = await client.post(PUBLIC_PATH, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def call(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    reply = await rpc(client, "tools/call", {"name": name, "arguments": arguments})
    text = reply["result"]["content"][0]["text"]
    # Neither a business token nor the member's CoreMan token ever comes back.
    assert "eyJ" not in text and "cmo_" not in text
    return json.loads(text)  # type: ignore[no-any-return]


async def test_member_catalog_is_filtered_and_proxied_as_the_member(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, member, grant, access = await setup(db_session)
    async with agent(app, access) as client:
        names = [t["name"] for t in (await rpc(client, "tools/list"))["result"]["tools"]]
        assert names[-2:] == ["systems_call", "systems_call_result"]
        systems = await call(client, "systems_browse", {})
        stock = systems["systems"][0]
        assert stock["call_mode"] == "proxy" and stock["permission_check"] == "filtered"
        # stock:report:read is not held: the daily report is left out.
        assert (await call(client, "systems_search", {"query": "每日 统计"}))["items"] == []
        hidden = await call(
            client, "systems_describe", {"system": "stock", "operation_id": "dailyReport"}
        )
        assert hidden["error"] == "operation_not_found"
        detail = await call(
            client, "systems_describe", {"system": "stock", "operation_id": "createDraft"}
        )
        assert detail["call"]["mode"] == "proxy" and detail["call"]["allowed"] is True
        assert "飞书" in detail["call"]["message"]

        listed = await call(
            client,
            "systems_call",
            {"system": "stock", "operation_id": "listDocuments", "query": {"status": "confirmed"}},
        )
        assert listed["ok"] is True and listed["data"]["data"]["items"][0]["id"] == "D1"

    # The business system saw a platform-issued token for the member, never the CoreMan token.
    sent = fake.requests[-1]
    assert sent.url.path == "/api/stock/documents"
    assert fake.token(sent) is not None and access not in str(sent.headers)
    assert fake.subject(sent) == "zhangsan"
    row = await db_session.scalar(select(SystemCall).order_by(SystemCall.id.desc()))
    assert row is not None and row.outcome == "ok"
    assert row.task_id is None and row.bot_id is None
    assert row.user_id == member.id and row.oauth_grant_id == grant.id
    issue = await db_session.scalar(
        select(BusinessTokenIssue).where(BusinessTokenIssue.oauth_grant_id.is_not(None))
    )
    assert issue is not None and issue.oauth_grant_id == grant.id and issue.task_id is None


async def test_denied_and_unsupported_calls(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, _, _, access = await setup(db_session)
    fake.permissions = ["*"]
    async with agent(app, access) as client:
        refund = await call(
            client, "systems_call", {"system": "stock", "operation_id": "refundSupplier"}
        )
        # Financial operations wait for the member's card like other writes.
        assert refund["status"] == "pending_confirmation"
        batch = await call(
            client,
            "systems_call",
            {"system": "stock", "operation_id": "createDraft", "batch": {"upload_id": "x"}},
        )
        assert batch["error"] == "unsupported_for_member"
        hidden = await call(
            client, "systems_call", {"system": "stock", "operation_id": "rebuildIndex"}
        )
        assert hidden["error"] == "operation_not_found"
        other = await call(client, "systems_call_result", {"confirmation_id": str(uuid.uuid4())})
        assert other["error"] == "confirmation_not_found"


async def test_permission_lookup_hides_operations_the_member_lacks(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, _, _, access = await setup(db_session)
    fake.permissions = ["stock:doc:read"]
    async with agent(app, access) as client:
        refused = await call(
            client,
            "systems_call",
            {"system": "stock", "operation_id": "createDraft", "body": {"sku": "A", "quantity": 1}},
        )
    assert refused["error"] == "operation_not_found"
    assert not any(r.method == "POST" for r in fake.requests)


async def test_direct_systems_stay_direct(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, _, _, access = await setup(db_session, delivery="env")
    async with agent(app, access) as client:
        names = [t["name"] for t in (await rpc(client, "tools/list"))["result"]["tools"]]
        assert "systems_call" not in names
        stock = (await call(client, "systems_browse", {}))["systems"][0]
        # Not proxied, but still filtered by what the member holds.
        assert stock["call_mode"] == "direct" and stock["permission_check"] == "filtered"
        detail = await call(
            client, "systems_describe", {"system": "stock", "operation_id": "listDocuments"}
        )
        assert detail["call"]["mode"] == "direct"
        refused = await call(
            client, "systems_call", {"system": "stock", "operation_id": "listDocuments"}
        )
        assert refused["error"] == "not_proxied"


def worker_ctx(app: Any) -> Any:
    return SimpleNamespace(
        cipher=app.state.cipher,
        settings_store=app.state.settings_store,
        external_jwt_key=None,
        business_token_providers={},
        task=SimpleNamespace(kind="card_action", status="running", cancel_requested_at=None),
    )


async def click(
    app: Any,
    db_session: AsyncSession,
    bot: Any,
    confirmation: str,
    verb: str,
    *,
    who: str = "ou_zs",
) -> str:
    inbound = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4()}",
        kind="card_action",
        chat_type="single",
        chat_id="oc_dm_zs",
        sender_platform_user_id=who,
        payload={},
        reply_context={"chat_id": "oc_dm_zs", "message_id": "om_card"},
    )
    db_session.add(inbound)
    await db_session.flush()
    action = {
        "task_id": member_writes.card_ref(uuid.UUID(confirmation)),
        "event_key": member_writes.CARD_EVENT,
        "level": f"{verb}:{confirmation}",
    }
    result = await member_write_cards.handle(db_session, worker_ctx(app), bot, inbound, action)
    await db_session.commit()
    return result


async def test_write_waits_for_the_members_click_on_the_card(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, member, _, access = await setup(db_session)
    db_session.add(User(login_name="lisi", display_name="李四", email="lisi@example.test"))
    await db_session.flush()
    lisi = await db_session.scalar(select(User).where(User.login_name == "lisi"))
    assert lisi is not None
    db_session.add(UserIdentity(user_id=lisi.id, platform="feishu", platform_user_id="ou_ls"))
    await db_session.commit()
    async with agent(app, access) as client:
        staged = await call(
            client,
            "systems_call",
            {
                "system": "stock",
                "operation_id": "createDraft",
                "body": {"sku": "SKU-9", "quantity": 3},
                "note": {"target": "SKU-9 入库草稿"},
            },
        )
        assert staged["status"] == "pending_confirmation"
        confirmation = staged["confirmation_id"]
        # Nothing reached the business system yet.
        assert not any(r.method == "POST" for r in fake.requests)
        card = await db_session.scalar(
            select(OutboxItem).where(OutboxItem.kind == "send", OutboxItem.bot_id == bot.id)
        )
        assert card is not None and card.target == {"user_id": "ou_zs"}
        flat = str(card.payload["card"])
        assert "来自你本机的 Claude Code" in flat and "SKU-9 入库草稿" in flat
        assert card.payload["card"]["task_id"] == member_writes.card_ref(uuid.UUID(confirmation))
        # Poll several times: each round rolls back the session, which must not lazy-load the
        # member afterwards (MissingGreenlet, a 500).
        monkeypatch.setattr(member_writes, "WAIT_SECONDS", 0.05)
        monkeypatch.setattr(member_writes, "POLL_SECONDS", 0.01)
        waiting = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert waiting["status"] == "pending"
        monkeypatch.setattr(member_writes, "WAIT_SECONDS", 0.0)

        # Someone else clicking does nothing; the member's click runs it once.
        assert await click(app, db_session, bot, confirmation, "confirm", who="ou_ls") == "ignored"
        assert await click(app, db_session, bot, confirmation, "confirm") == "member_write_ok"
        assert await click(app, db_session, bot, confirmation, "confirm") == "ignored"
        posts = [r for r in fake.requests if r.method == "POST"]
        assert len(posts) == 1 and json.loads(posts[0].content) == {"sku": "SKU-9", "quantity": 3}
        assert fake.subject(posts[0]) == "zhangsan"

        done = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert done["status"] == "ok" and "SKU-9" in done["response"]

    outcomes = [
        row.outcome
        for row in await db_session.scalars(
            select(SystemCall).where(SystemCall.user_id == member.id).order_by(SystemCall.id)
        )
    ]
    assert outcomes == ["proposed", "confirmed_ok"]
    updated = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update").order_by(OutboxItem.id.desc())
    )
    assert updated is not None and "已完成" in str(updated.payload["card"])


def _buttons(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        found = [node] if node.get("tag") == "button" else []
        return found + [b for v in node.values() for b in _buttons(v)]
    if isinstance(node, list):
        return [b for v in node for b in _buttons(v)]
    return []


async def test_confirm_click_runs_through_gateway_and_worker(
    app: Any, db_session: AsyncSession, db_engine: AsyncEngine, fake: FakeBusinessSystem
) -> None:
    """The real path: the card's own button value, normalized and admitted by the Feishu gateway,
    then run by the worker's card-action handler."""
    bot, _, _, access = await setup(db_session)
    body = {"system": "stock", "operation_id": "createDraft", "body": {"sku": "E2E", "quantity": 2}}
    async with agent(app, access) as client:
        confirmation = (await call(client, "systems_call", body))["confirmation_id"]
        card = await db_session.scalar(
            select(OutboxItem).where(OutboxItem.kind == "send", OutboxItem.bot_id == bot.id)
        )
        assert card is not None
        card.status = "sent"
        card.payload = {**card.payload, "_feishu_message_id": "om_member_card"}
        await db_session.commit()
        [value] = [
            b["behaviors"][0]["value"]
            for b in _buttons(card.payload["card"])
            if str(b["behaviors"][0]["value"].get("level", "")).startswith("confirm:")
        ]
        raw = {
            "header": {
                "app_id": "cli_a",
                "event_type": "card.action.trigger",
                "event_id": "ev_member_confirm",
                "tenant_key": "tenant",
            },
            "event": {
                "operator": {"user_id": "ou_zs", "open_id": "ou_open_zs"},
                "context": {"open_chat_id": "oc_dm_zs", "open_message_id": "om_member_card"},
                "action": {"tag": "button", "value": value},
            },
        }
        message = normalize_event(
            raw,
            bot_id=bot.id,
            app_id="cli_a",
            bot_open_id="ou_bot",
            gateway_instance="gw",
            now=datetime.now(UTC),
        )
        assert message is not None
        factory = make_session_factory(db_engine)
        async with factory() as session:
            outcome = await admit(session, bot, message, lease_generation=1)
            await session.commit()
        assert outcome == "write_confirm"
        task = await db_session.scalar(select(Task).where(Task.kind == "card_action"))
        assert task is not None
        task.status = "running"
        await db_session.commit()
        await CardActionHandler().run(build_ctx(db_engine, task))
        finished = await db_session.get(Task, task.id, populate_existing=True)
        assert finished is not None and finished.result == {"card": "member_write_ok"}
        posts = [r for r in fake.requests if r.method == "POST"]
        assert len(posts) == 1 and json.loads(posts[0].content) == body["body"]
        done = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert done["status"] == "ok"


async def test_cancel_and_revoked_grant(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    bot, _, grant, access = await setup(db_session)
    body = {"system": "stock", "operation_id": "createDraft", "body": {"sku": "A", "quantity": 1}}
    async with agent(app, access) as client:
        first = (await call(client, "systems_call", body))["confirmation_id"]
        second = (await call(client, "systems_call", body))["confirmation_id"]
        assert await click(app, db_session, bot, first, "cancel") == "member_write_cancelled"
        cancelled = await call(client, "systems_call_result", {"confirmation_id": first})
        assert cancelled["status"] == "cancelled"
    # The member revoked this agent's access before confirming: nothing runs.
    grant = await db_session.get(OAuthGrant, grant.id, populate_existing=True)
    assert grant is not None
    grant.revoked_at = utcnow()
    await db_session.commit()
    assert await click(app, db_session, bot, second, "confirm") == "member_write_unavailable"
    state = await db_session.get(InteractionState, uuid.UUID(second), populate_existing=True)
    assert state is not None and state.state["result"]["status"] == "refused"
    assert not any(r.method == "POST" for r in fake.requests)


async def test_write_without_public_bot_is_unavailable(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, _, _, access = await setup(db_session, public_bot=False)
    async with agent(app, access) as client:
        refused = await call(
            client,
            "systems_call",
            {"system": "stock", "operation_id": "createDraft", "body": {"sku": "A", "quantity": 1}},
        )
    assert refused["error"] == "confirmation_unavailable"
    assert await db_session.scalar(select(InteractionState.id)) is None


async def test_rate_limit_and_hourly_budget(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, access = await setup(db_session)
    system = await db_session.get(BusinessSystem, "stock")
    assert system is not None
    system.proxy_calls_per_turn = 1
    await db_session.commit()
    read = {"system": "stock", "operation_id": "listDocuments", "query": {"status": "draft"}}
    async with agent(app, access) as client:
        assert (await call(client, "systems_call", read))["ok"] is True
        stop = await call(client, "systems_call", read)
        assert stop["error"] == "catalog_budget_exhausted"
        monkeypatch.setattr(router, "_member_limit", router._RateLimit(1))
        await call(client, "systems_browse", {})
        limited = await rpc(client, "tools/call", {"name": "systems_browse", "arguments": {}})
        assert limited["error"]["code"] == -32000


TASKS = "io.modelcontextprotocol/tasks"


async def modern(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    tasks: bool = True,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """One request of the stateless protocol revision: self-describing `_meta`, mirrored headers."""
    capabilities = {"extensions": {TASKS: {}}} if tasks else {}
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": capabilities,
    }
    params = params or {}
    sent = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    named = params.get("name") or params.get("taskId")
    if named:
        sent["Mcp-Name"] = named
    return await client.post(
        PUBLIC_PATH,
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": {**params, "_meta": meta}},
        headers={**sent, **(headers or {})},
    )


async def test_stateless_revision_discovery_and_headers(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    _, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        found = (await modern(client, "server/discover")).json()["result"]
        assert found["resultType"] == "complete"
        assert found["supportedVersions"] == ["2026-07-28"]
        assert found["capabilities"] == {"tools": {}, "extensions": {TASKS: {}}}
        assert found["ttlMs"] > 0 and found["cacheScope"] == "public"
        assert found["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "coreman-systems"

        listed = (await modern(client, "tools/list")).json()["result"]
        # The list depends on the member, so nobody may share or keep it.
        assert listed["resultType"] == "complete"
        assert (listed["ttlMs"], listed["cacheScope"]) == (0, "private")
        hints = {tool["name"]: tool["annotations"] for tool in listed["tools"]}
        assert hints["systems_search"]["readOnlyHint"] is True
        assert hints["systems_call"] == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": True,
        }

        # The handshake and keep-alive of older revisions are not methods here.
        for gone in ("initialize", "ping"):
            reply = await modern(client, gone)
            assert reply.status_code == 404 and reply.json()["error"]["code"] == -32601
        mismatch = await modern(
            client, "tools/call", {"name": "systems_browse"}, headers={"Mcp-Name": "other"}
        )
        assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32020
        encoded = await modern(
            client,
            "tools/call",
            {"name": "systems_browse", "arguments": {}},
            headers={"Mcp-Name": "=?base64?c3lzdGVtc19icm93c2U=?="},
        )
        assert encoded.status_code == 200 and "error" not in encoded.json()
        wrong = await modern(client, "tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
        assert wrong.status_code == 400 and wrong.json()["error"]["code"] == -32020
        old = await client.post(
            PUBLIC_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2030-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            headers={"MCP-Protocol-Version": "2030-01-01", "Mcp-Method": "tools/list"},
        )
        assert old.status_code == 400
        assert old.json()["error"]["code"] == -32022
        assert old.json()["error"]["data"]["supported"][0] == "2026-07-28"
        missing = await client.post(
            PUBLIC_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
        )
        assert missing.status_code == 400 and missing.json()["error"]["code"] == -32602

        # Clients of older revisions are served as before, on the same endpoint.
        legacy = await rpc(client, "initialize", {"protocolVersion": "2025-11-25"})
        assert legacy["result"]["protocolVersion"] == "2025-11-25"
        assert "resultType" not in (await rpc(client, "tools/list"))["result"]
        assert (await rpc(client, "ping"))["result"] == {}
        cancelled = await client.post(
            PUBLIC_PATH,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
        )
        assert cancelled.status_code == 202
        assert (await rpc(client, "tasks/get", {"taskId": "x"}))["error"]["code"] == -32601


async def test_staged_write_is_a_task_for_clients_that_support_tasks(
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    bot, _, _, access = await setup(db_session)
    write = {
        "name": "systems_call",
        "arguments": {
            "system": "stock",
            "operation_id": "createDraft",
            "body": {"sku": "SKU-9", "quantity": 3},
            "note": {"target": "SKU-9 入库草稿"},
        },
    }
    async with agent(app, access) as client:
        # Without the extension on the request the write is staged the old way.
        plain = (await modern(client, "tools/call", write, tasks=False)).json()["result"]
        assert plain["resultType"] == "complete"
        assert json.loads(plain["content"][0]["text"])["status"] == "pending_confirmation"
        refused = await modern(client, "tasks/get", {"taskId": str(uuid.uuid4())}, tasks=False)
        assert refused.status_code == 400 and refused.json()["error"]["code"] == -32021

        created = (await modern(client, "tools/call", write)).json()["result"]
        assert created["resultType"] == "task" and created["status"] == "working"
        assert created["ttlMs"] > 0 and created["pollIntervalMs"] == 3000
        assert created["createdAt"] and created["lastUpdatedAt"]
        task_id = created["taskId"]
        assert not any(r.method == "POST" for r in fake.requests)

        waiting = (await modern(client, "tasks/get", {"taskId": task_id})).json()["result"]
        assert waiting["resultType"] == "complete" and waiting["status"] == "working"
        assert "result" not in waiting and "确认" in waiting["statusMessage"]
        # Nothing the agent sends can confirm or withdraw the write.
        for method in ("tasks/update", "tasks/cancel"):
            params = {"taskId": task_id, **({"inputResponses": {}} if "update" in method else {})}
            assert (await modern(client, method, params)).json()["result"] == {
                "resultType": "complete",
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "coreman-systems",
                        "version": "2.0",
                    }
                },
            }
        assert not any(r.method == "POST" for r in fake.requests)
        unknown = (await modern(client, "tasks/get", {"taskId": str(uuid.uuid4())})).json()
        assert unknown["error"]["code"] == -32602

        assert await click(app, db_session, bot, task_id, "confirm") == "member_write_ok"
        done = (await modern(client, "tasks/get", {"taskId": task_id})).json()["result"]
        assert done["status"] == "completed" and done["result"]["isError"] is False
        outcome = json.loads(done["result"]["content"][0]["text"])
        assert outcome["status"] == "ok" and "SKU-9" in outcome["response"]

    # Another member's agent cannot see the task.
    other = User(login_name="lisi", display_name="李四", email="lisi@example.test")
    db_session.add(other)
    db_session.add(OAuthClient(client_id="cli-other", client_name="Codex", redirect_uris=[]))
    await db_session.flush()
    grant = OAuthGrant(
        client_id="cli-other",
        user_id=other.id,
        scope="mcp",
        refresh_hash=uuid.uuid4().hex,
        refresh_expires_at=utcnow() + timedelta(days=30),
    )
    db_session.add(grant)
    await db_session.flush()
    theirs = await oauth._mint_access(db_session, grant)
    await db_session.commit()
    async with agent(app, theirs) as client:
        hidden = (await modern(client, "tasks/get", {"taskId": task_id})).json()
        assert hidden["error"]["code"] == -32602
