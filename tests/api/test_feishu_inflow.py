"""用到飞书能力时自动发授权卡。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import respx
from sqlalchemy import select

from coreman.core.db.models import FeishuAuthWait, FeishuPersonalGrant, OutboxItem
from coreman.core.feishu_personal import inflow, policy, service
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat import personal
from tests.api.test_feishu_personal import URL, grant, headers, rpc, setup, value

DEVICE = "https://accounts.feishu.cn/oauth/v1/device_authorization"


def device(mock):
    return mock.post(DEVICE).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "device",
                "verification_uri_complete": "https://accounts.feishu.cn/verify",
                "expires_in": 600,
                "interval": 5,
            },
        )
    )


async def private_scope(session, task, user):
    return await policy.task_scope(session, task.id, str(user.id))


async def test_first_use_sends_a_one_click_card(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()

    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        device(mock)
        scope = await private_scope(db_session, task, user)
        out = await inflow.request(db_session, app.state.cipher, scope)
    assert out["status"] == "authorization_sent"
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    assert wait.status == "waiting" and wait.origin_task_id == task.id
    item = (await db_session.scalars(select(OutboxItem))).one()
    assert "https://accounts.feishu.cn/verify" in str(item.payload["card"])
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert row.authorization_level == "all_except_send" and row.status == "pending"


async def test_upgrade_keeps_the_old_token_until_clicked(app, db_session):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="messages_readonly")
    await db_session.commit()
    scope = await private_scope(db_session, task, user)
    out = await inflow.request(db_session, app.state.cipher, scope, need="general")
    assert out["status"] == "upgrade_offered"
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert row.status == "connected" and row.token_enc
    assert (await db_session.scalars(select(FeishuAuthWait))).one().status == "offered"


async def test_a_live_wait_does_not_send_a_second_card(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()

    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        route = device(mock)
        scope = await private_scope(db_session, task, user)
        await inflow.request(db_session, app.state.cipher, scope)
        await inflow.request(db_session, app.state.cipher, scope)
    assert route.call_count == 1
    assert len(list(await db_session.scalars(select(OutboxItem)))) == 1


def test_need_maps_to_tiers():
    class Row:
        status, token_enc = "connected", "x"

    row = Row()
    row.authorization_level = "all_except_send"
    assert inflow.satisfied(row, "general") and not inflow.satisfied(row, "send_messages")
    row.authorization_level = "messages_readonly"
    assert inflow.satisfied(row, "read_messages") and not inflow.satisfied(row, "general")
    row.authorization_level = "no_messages"
    assert inflow.satisfied(row, "general") and not inflow.satisfied(row, "read_messages")


async def test_upgrade_click_is_bound_to_the_waiting_owner(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="messages_readonly")
    await db_session.commit()
    scope = await private_scope(db_session, task, user)
    await inflow.request(db_session, app.state.cipher, scope)
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    action = {"level": str(wait.id), "event_key": inflow.UPGRADE_EVENT}
    ctx = SimpleNamespace(cipher=app.state.cipher)

    def click(sender, tenant="tenant-test"):
        return SimpleNamespace(
            id=1,
            chat_id="oc_private",
            reply_context={"message_id": "om_card"},
            sender_platform_user_id=sender,
            sender_open_id="ou_" + sender,
            payload={"raw": {"header": {"tenant_key": tenant}}},
        )

    out = await inflow.handle_upgrade(db_session, ctx, bot, click("stranger"), action)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert out == "ignored" and row.status == "connected" and row.token_enc
    assert wait.status == "offered"

    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        device(mock)
        mock.post("https://accounts.feishu.cn/oauth/v1/revoke").mock(
            return_value=httpx.Response(200, json={})
        )
        out = await inflow.handle_upgrade(db_session, ctx, bot, click("human"), action)
    assert out == "personal_authorization_pending" and wait.status == "waiting"


def _stub_scopes(monkeypatch):
    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)


def _revoke(mock):
    mock.post("https://accounts.feishu.cn/oauth/v1/revoke").mock(
        return_value=httpx.Response(200, json={})
    )


async def test_a_live_flow_elsewhere_sends_no_second_card(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()
    _stub_scopes(monkeypatch)
    with respx.mock as mock:
        device(mock)
        scope = await private_scope(db_session, task, user)
        await inflow.request(db_session, app.state.cipher, scope)
        other = replace(scope, chat_id="oc_other")
        out = await inflow.request(db_session, app.state.cipher, other)
    assert out["status"] == "authorization_sent"
    assert len(list(await db_session.scalars(select(OutboxItem)))) == 1
    waits = list(await db_session.scalars(select(FeishuAuthWait)))
    assert {w.chat_id for w in waits} == {"oc_private", "oc_other"}


async def test_an_expired_device_flow_issues_a_fresh_card(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()
    _stub_scopes(monkeypatch)
    with respx.mock as mock:
        route = device(mock)
        scope = await private_scope(db_session, task, user)
        await inflow.request(db_session, app.state.cipher, scope)
        row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
        row.pending_expires_at = utcnow() - timedelta(minutes=1)
        await db_session.flush()
        out = await inflow.request(db_session, app.state.cipher, scope)
    assert out["status"] == "authorization_sent" and route.call_count == 2
    assert len(list(await db_session.scalars(select(OutboxItem)))) == 2
    assert len(list(await db_session.scalars(select(FeishuAuthWait)))) == 1


async def test_a_double_click_keeps_the_link(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="messages_readonly")
    await db_session.commit()
    scope = await private_scope(db_session, task, user)
    await inflow.request(db_session, app.state.cipher, scope)
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    action = {"level": str(wait.id), "event_key": inflow.UPGRADE_EVENT}
    ctx = SimpleNamespace(cipher=app.state.cipher)
    _stub_scopes(monkeypatch)
    with respx.mock as mock:
        route = device(mock)
        _revoke(mock)
        for n in (1, 2):
            click = SimpleNamespace(
                id=n,
                chat_id="oc_private",
                reply_context={"message_id": "om_card"},
                sender_platform_user_id="human",
                sender_open_id="ou_human",
                payload={"raw": {"header": {"tenant_key": "tenant-test"}}},
            )
            out = await inflow.handle_upgrade(db_session, ctx, bot, click, action)
            assert out == "personal_authorization_pending"
    assert route.call_count == 1


async def test_a_request_error_leaves_no_wait(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()

    async def scopes(*a, **k):
        return []

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    scope = await private_scope(db_session, task, user)
    out = await inflow.request(db_session, app.state.cipher, scope)
    assert out["status"] == "app_permission_missing"
    assert list(await db_session.scalars(select(FeishuAuthWait))) == []
    assert list(await db_session.scalars(select(OutboxItem))) == []


async def test_a_click_from_another_tenant_is_ignored(app, db_session):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="messages_readonly")
    await db_session.commit()
    scope = await private_scope(db_session, task, user)
    await inflow.request(db_session, app.state.cipher, scope)
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    card = (await db_session.scalars(select(OutboxItem))).one().payload["card"]
    value = card["body"]["elements"][1]["behaviors"][0]["value"]
    click = SimpleNamespace(
        id=1,
        chat_id="oc_private",
        reply_context={"message_id": "om_card"},
        sender_platform_user_id="human",
        sender_open_id="ou_human",
        payload={"raw": {"header": {"tenant_key": "other-tenant"}}},
    )
    out = await inflow.handle_upgrade(
        db_session, SimpleNamespace(cipher=app.state.cipher), bot, click, value
    )
    assert out == "ignored" and wait.status == "offered"


async def test_a_group_origin_card_goes_to_the_user_only(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()
    _stub_scopes(monkeypatch)
    scope = await private_scope(db_session, task, user)
    scope.event.chat_type = "group"
    scope.event.chat_id = "oc_group"
    with respx.mock as mock:
        device(mock)
        await inflow.request(db_session, app.state.cipher, scope)
    item = (await db_session.scalars(select(OutboxItem))).one()
    assert item.target == {"user_id": "human"}
    fallback = item.payload["_dm_fallback"]
    assert fallback["chat_id"] == "oc_group" and "http" not in str(fallback)
    assert "accounts.feishu.cn" in str(item.payload["card"])
    rows = [i for i in await db_session.scalars(select(OutboxItem)) if "chat_id" in i.target]
    assert not any("accounts.feishu.cn" in str(i.payload.get("card")) for i in rows)


async def test_one_click_releases_every_chat_waiting_for_the_upgrade(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="messages_readonly")
    await db_session.commit()
    _stub_scopes(monkeypatch)
    scope = await private_scope(db_session, task, user)
    await inflow.request(db_session, app.state.cipher, scope)
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    elsewhere = FeishuAuthWait(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_group",
        origin_task_id=task.id,
        need="general",
        status="offered",
        expires_at=utcnow() + inflow.WAIT_TTL,
    )
    stale = FeishuAuthWait(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_old",
        origin_task_id=task.id,
        need="general",
        status="offered",
        expires_at=utcnow() - timedelta(minutes=1),
    )
    db_session.add_all([elsewhere, stale])
    await db_session.commit()
    click = SimpleNamespace(
        id=1,
        chat_id="oc_private",
        reply_context={"message_id": "om_card"},
        sender_platform_user_id="human",
        sender_open_id="ou_human",
        payload={"raw": {"header": {"tenant_key": "tenant-test"}}},
    )
    action = {"level": str(wait.id), "event_key": inflow.UPGRADE_EVENT}
    with respx.mock as mock:
        device(mock)
        _revoke(mock)
        out = await inflow.handle_upgrade(
            db_session, SimpleNamespace(cipher=app.state.cipher), bot, click, action
        )
    assert out == "personal_authorization_pending"
    await db_session.refresh(elsewhere)
    await db_session.refresh(stale)
    assert wait.status == "waiting" and elsewhere.status == "waiting"
    assert stale.status == "offered"


async def _no_grant_headers(app, task, user):
    from coreman.core.chat import sessions

    async with app.state.session_factory() as session:
        info = await sessions.get_or_create(
            session,
            bot_id=task.bot_id,
            session_key=task.session_key,
            backend="claude",
            ttl_hours=72,
            speaker_user_id=user.id,
        )
        await session.commit()
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        context_epoch=policy.NO_GRANT_EPOCH,
        base_session_id=info.relay_session_id,
        ttl_seconds=1800,
    )
    return {"Authorization": "Bearer " + token}


async def test_private_chat_authorize_tool_sends_the_card(client, app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await db_session.commit()

    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        device(mock)
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("feishu_authorize", {"purpose": "查日程"}),
            )
        )
    assert out["status"] == "authorization_sent"


async def test_not_yet_connected_user_can_authorize_with_the_no_grant_capability(
    client, app, db_session, monkeypatch
):
    bot, user, task = await setup(db_session, app)
    # setup 不建授权记录，首轮能力里带的是「无授权」纪元。
    assert await db_session.get(FeishuPersonalGrant, (bot.id, user.id)) is None
    await db_session.commit()
    auth = await _no_grant_headers(app, task, user)

    async def scopes(*a, **k):
        return ["docx:document", "offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        device(mock)
        out = value(await client.post(URL, headers=auth, json=rpc("feishu_authorize")))
    assert out["status"] == "authorization_sent"
    # 发起授权换了纪元，本轮旧能力不再有效；续接时配置阶段会签发新能力。
    again = value(await client.post(URL, headers=auth, json=rpc("feishu_authorization_status")))
    assert again["error"] == "authorization_changed"


async def test_fresh_capability_after_resume_reaches_data_tools(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    names = {
        tool["name"]
        for tool in (
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        ).json()["result"]["tools"]
    }
    assert "feishu_search_messages" in names


async def test_authorize_arguments_are_validated(client, app, db_session):
    _, user, task = await setup(db_session, app)
    for args in ({"need": "everything"}, {"level": "all"}, {"purpose": "x" * 61}):
        out = value(
            await client.post(
                URL, headers=await headers(app, task, user), json=rpc("feishu_authorize", args)
            )
        )
        assert out["error"] == "invalid_tool_or_arguments"


def test_guidance_names_the_tool_and_is_stable_across_states():
    connected = SimpleNamespace(status="connected", token_enc=b"x", authorization_level="all")
    pending = SimpleNamespace(
        status="pending",
        token_enc=None,
        pending_expires_at=utcnow() + timedelta(minutes=5),
        authorization_level="all",
    )
    rules = [
        personal.feishu_guidance(row, "https://example.com", scheduled=False)[0]
        for row in (None, pending, connected)
    ]
    assert rules[0] == rules[1] == rules[2]
    assert "feishu_authorize" in rules[0] and "请引导用户在私聊里发送“连接飞书”" not in rules[0]
    assert personal.feishu_guidance(None, "https://example.com", scheduled=True) == ("", "")
