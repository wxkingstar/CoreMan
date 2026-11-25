"""A member's own agent on the catalog MCP: filtered by the member's permissions, proxied calls
as the member, writes confirmed on a Feishu card from the public bot."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    User,
    UserIdentity,
)
from coreman.core.db.models.oauth import OAuthClient, OAuthGrant
from coreman.core.oauth import server as oauth
from coreman.core.systems_catalog import member_writes, service
from coreman.core.timeutils import utcnow
from coreman.core.work_tasks.cards import NOTIFIER_SETTING
from coreman.runtime.worker import member_write_cards
from tests.api.test_systems_catalog import bot_cipher, turn
from tests.fakes.business_system import FakeBusinessSystem, install


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
        assert refund["error"] == "risk_not_allowed"
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
    app: Any, db_session: AsyncSession, fake: FakeBusinessSystem
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
        waiting = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert waiting["status"] == "pending"

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
