"""用到飞书能力时自动发授权卡。"""

from types import SimpleNamespace

import httpx
import respx
from sqlalchemy import select

from coreman.core.db.models import FeishuAuthWait, FeishuPersonalGrant, OutboxItem
from coreman.core.feishu_personal import inflow, policy, service
from tests.api.test_feishu_personal import grant, setup

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

    def click(sender):
        return SimpleNamespace(
            id=1,
            chat_id="oc_private",
            reply_context={"message_id": "om_card"},
            sender_platform_user_id=sender,
            sender_open_id="ou_" + sender,
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
