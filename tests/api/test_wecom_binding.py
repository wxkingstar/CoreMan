"""「我的企业微信」：扫码绑定本人的授权机器人、核对是本人、检测各项能力、档位与解除绑定。"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from coreman.core.db.models import (
    Bot,
    OutboxItem,
    User,
    UserIdentity,
    UserReached,
    WecomPersonalBinding,
)
from coreman.core.db.session import make_session_factory
from coreman.core.wecom_personal import binding, gateway, reminders, service
from tests.api.conftest import login_existing
from tests.api.test_wecom_bot_provisions import _bound, _fake
from tests.api.test_wecom_personal import (
    PERSONAL_BOT,
    PERSONAL_SECRET,
    SENDER,
    bind,
    context,
    envelope,
)
from tests.integration.worker_helpers import seed_bot

BASE = "/api/me/wecom-binding"


async def member(client, db_session, *, linked=True):
    user = User(login_name="binder", display_name="绑定人", source="sync")
    db_session.add(user)
    await db_session.flush()
    if linked:
        db_session.add(
            UserIdentity(
                user_id=user.id, platform="wecom", platform_user_id="binder", open_id=SENDER
            )
        )
    await db_session.commit()
    await login_existing(client, db_session, user)
    return user


def mock_wecom(mock, *, authorizer=SENDER, probe=None):
    """换令牌、whoami，以及各项能力的只读检测；probe 指定某项返回的错误码。"""
    mock.post(gateway.AUTH_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "token": "tok-bind"})
    )
    mock.post(gateway.BASE_URL + "/identity/whoami").mock(
        return_value=envelope({"extra_identity_context": context(authorizer)})
    )
    routes = {}
    for service_name, (path, _) in service.probe_calls(
        WecomPersonalBinding(authorizer_name="本人")
    ).items():
        errcode = (probe or {}).get(service_name)
        response = (
            httpx.Response(200, json={"errcode": errcode, "errmsg": "x"})
            if errcode
            else envelope({"items": []})
        )
        routes[service_name] = mock.post(gateway.BASE_URL + path).mock(return_value=response)
    return routes


async def test_unbound_state_says_whether_the_account_is_linked(client, db_session):
    await member(client, db_session, linked=False)
    data = (await client.get(BASE)).json()["data"]
    assert data["status"] == "unbound" and data["identity_linked"] is False
    assert data["scan"] is None and data["capabilities"] == [] and data["auth_ttl_days"] == 7
    started = await client.post(BASE + "/scan")
    assert started.status_code == 422 and "同步" in started.json()["message"]


async def test_scanning_requires_csrf(client, db_session, monkeypatch):
    _fake(monkeypatch)
    await member(client, db_session)
    client.headers.pop("X-CSRF-Token")
    assert (await client.post(BASE + "/scan")).status_code == 403


async def test_scan_binds_the_member_and_probes_every_capability(
    client, db_session, app, monkeypatch
):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    started = (await client.post(BASE + "/scan")).json()["data"]
    assert started["scan"]["status"] == "pending" and started["scan"]["url"].startswith("https://")
    assert started["scan"]["retry_after"] == binding.POLL_SECONDS
    # 有效期内再点一次复用同一个二维码。
    again = (await client.post(BASE + "/scan")).json()["data"]
    assert again["scan"]["url"] == started["scan"]["url"] and len(fake.generated) == 1

    row = await db_session.get(WecomPersonalBinding, user.id)
    row.scan_next_poll_at = None
    await db_session.commit()
    waiting = (await client.get(BASE + "/scan")).json()["data"]
    assert waiting["scan"]["status"] == "pending" and waiting["status"] == "unbound"

    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        routes = mock_wecom(mock, probe={"mail": 850002})
        done = (await client.get(BASE + "/scan")).json()["data"]
        assert all(route.called for route in routes.values())
    assert done["status"] == "bound" and done["scan"]["status"] == "succeeded"
    assert done["scan"]["url"] is None
    assert done["bot_name"] == "示例" and done["authorizer_name"] == "本人"
    by_service = {item["service"]: item for item in done["capabilities"]}
    assert by_service["todo"]["read"]["state"] == "ok"
    assert by_service["todo"]["read"]["expires_at"] and done["next_expiry"]
    assert by_service["mail"]["read"]["state"] == "unauthorized"
    assert by_service["todo"]["write"] is None
    text = json.dumps(done)
    assert PERSONAL_SECRET not in text and "tok-bind" not in text
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.scan_enc is None and row.wecom_bot_id == PERSONAL_BOT
    assert service.credentials(app.state.cipher, row) == (PERSONAL_BOT, PERSONAL_SECRET)


@pytest.mark.parametrize(
    ("authorizer", "error"),
    [("wo-someone-else-0000000000000000000", "identity_unlinked"), (None, "not_authorized")],
)
async def test_a_bot_that_does_not_represent_the_member_is_discarded(
    client, db_session, monkeypatch, authorizer, error
):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    await client.post(BASE + "/scan")
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row = await db_session.get(WecomPersonalBinding, user.id)
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        mock_wecom(mock, authorizer=authorizer)
        data = (await client.get(BASE + "/scan")).json()["data"]
    assert data["status"] == "unbound"
    assert data["scan"]["status"] == "failed" and data["scan"]["error"] == error
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.credentials_enc is None and row.scan_enc is None


async def test_someone_else_scanning_the_code_is_refused(client, db_session, monkeypatch):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=other.id, platform="wecom", platform_user_id="other", open_id="wo-other-000000"
        )
    )
    await db_session.commit()
    await client.post(BASE + "/scan")
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row = await db_session.get(WecomPersonalBinding, user.id)
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        mock_wecom(mock, authorizer="wo-other-000000")
        data = (await client.get(BASE + "/scan")).json()["data"]
    assert data["scan"]["error"] == "not_self" and data["status"] == "unbound"


async def test_verification_is_retried_when_wecom_is_briefly_unreachable(
    client, db_session, monkeypatch
):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    await client.post(BASE + "/scan")
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row = await db_session.get(WecomPersonalBinding, user.id)
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        mock.post(gateway.AUTH_URL).mock(return_value=httpx.Response(502))
        data = (await client.get(BASE + "/scan")).json()["data"]
    assert data["scan"]["status"] == "pending" and data["scan"]["error"] == "upstream_unavailable"
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.scan_upstream_status == "success" and row.scan_enc
    # 同一个 scode 再查一次会拿回同一份凭证，这次核对成功。
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        mock_wecom(mock)
        data = (await client.get(BASE + "/scan")).json()["data"]
    assert data["status"] == "bound"


async def test_cancel_checks_once_more_before_giving_up(client, db_session, monkeypatch):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    await client.post(BASE + "/scan")
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    with respx.mock as mock:
        mock_wecom(mock)
        data = (await client.delete(BASE + "/scan")).json()["data"]
    # 刚扫完码就关窗口：绑定照样完成，建好的授权机器人不会白白丢掉。
    assert data["status"] == "bound" and data["scan"]["status"] == "succeeded"
    # 刚生成过又取消，10 秒内不能反复生成新的二维码。
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    row.scan_expires_at = datetime.now(UTC) - binding.QR_TTL
    await db_session.commit()
    await client.post(BASE + "/scan")
    data = (await client.delete(BASE + "/scan")).json()["data"]
    assert data["scan"]["status"] == "cancelled" and data["status"] == "bound"
    assert (await client.post(BASE + "/scan")).status_code == 429
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.scan_enc is None


async def test_sweep_binds_members_who_left_the_page(
    client, db_session, db_engine, app, monkeypatch
):
    fake = _fake(monkeypatch)
    user = await member(client, db_session)
    await client.post(BASE + "/scan")
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row = await db_session.get(WecomPersonalBinding, user.id)
    row.scan_next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        mock_wecom(mock)
        polled = await binding.sweep(make_session_factory(db_engine), app.state.cipher)
    assert polled == 1
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.status == "bound" and row.scan_status == "succeeded"


async def test_level_and_pause_are_managed_on_the_page(client, db_session, app):
    user = await member(client, db_session)
    assert (await client.patch(BASE, json={"enabled": False})).status_code == 409
    row = await bind(db_session, app, user)
    epoch = row.context_epoch
    data = (await client.patch(BASE, json={"authorization_level": "all"})).json()["data"]
    assert data["authorization_level"] == "all" and data["enabled"] is True
    data = (await client.patch(BASE, json={"enabled": False})).json()["data"]
    assert data["enabled"] is False
    assert (await client.patch(BASE, json={"authorization_level": "root"})).status_code == 422
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.context_epoch != epoch


async def test_check_and_renewed_refresh_the_estimate(client, db_session, app):
    user = await member(client, db_session)
    old = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    await bind(
        db_session,
        app,
        user,
        authorizer_name="本人",
        capabilities={"todo:read": {"state": "ok", "authorized_at": old}},
    )
    with respx.mock as mock:
        mock_wecom(mock)
        checked = (await client.post(BASE + "/check")).json()["data"]
    todo = next(item for item in checked["capabilities"] if item["service"] == "todo")
    # 一直能用的能力不会因为检测而顺延：企业微信的授权调用不续期。
    assert datetime.fromisoformat(todo["read"]["expires_at"]) < datetime.now(UTC) + timedelta(
        days=2
    )
    with respx.mock as mock:
        mock_wecom(mock)
        renewed = (await client.post(BASE + "/renewed")).json()["data"]
    todo = next(item for item in renewed["capabilities"] if item["service"] == "todo")
    assert datetime.fromisoformat(todo["read"]["expires_at"]) > datetime.now(UTC) + timedelta(
        days=6
    )


async def test_check_reports_a_deleted_bot(client, db_session, app):
    user = await member(client, db_session)
    await bind(db_session, app, user, verified_at=datetime.now(UTC) - timedelta(hours=1))
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/identity/whoami").mock(
            return_value=httpx.Response(200, json={"errcode": 853005, "errmsg": "invalid"})
        )
        mock.post(gateway.AUTH_URL).mock(
            return_value=httpx.Response(200, json={"errcode": 853000, "errmsg": "invalid"})
        )
        data = (await client.post(BASE + "/check")).json()["data"]
    assert data["status"] == "unbound" and data["error"] == "credentials_rejected"


async def test_unbinding_deletes_the_credentials(client, db_session, app):
    user = await member(client, db_session)
    row = await bind(db_session, app, user)
    epoch = row.context_epoch
    data = (await client.delete(BASE)).json()["data"]
    assert data["status"] == "unbound" and data["wecom_bot_id"] is None
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.credentials_enc is None and row.token_enc is None and row.context_epoch != epoch


def test_first_success_counts_from_the_binding_and_recovery_from_now():
    bound = datetime.now(UTC) - timedelta(days=2)
    row = WecomPersonalBinding(bound_at=bound, capabilities={})
    service.record(row, "todo:read")
    assert datetime.fromisoformat(row.capabilities["todo:read"]["authorized_at"]) == bound
    service.record(row, "todo:read", errcode=850003, help_url="https://work.weixin.qq.com/x")
    assert row.capabilities["todo:read"]["state"] == "expired"
    assert row.capabilities["todo:read"]["renew_url"] == "https://work.weixin.qq.com/x"
    service.record(row, "todo:read")
    entry = row.capabilities["todo:read"]
    assert entry["state"] == "ok" and "renew_url" not in entry
    assert datetime.fromisoformat(entry["authorized_at"]) > datetime.now(UTC) - timedelta(minutes=1)


async def test_reminders_go_to_the_latest_private_chat_once(db_session, db_engine, app):
    bot, _, _ = await seed_bot(db_session)
    user = User(login_name="remind", display_name="提醒", source="sync")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="chat-remind"))
    soon = (datetime.now(UTC) - timedelta(days=6, hours=12)).isoformat()
    await bind(
        db_session,
        app,
        user,
        capabilities={"todo:read": {"state": "ok", "authorized_at": soon}},
    )
    factory = make_session_factory(db_engine)
    assert await reminders.remind(factory, "https://coreman.example.com") == 1
    item = await db_session.scalar(select(OutboxItem).where(OutboxItem.bot_id == bot.id))
    assert item.target == {"chat_id": "chat-remind"}
    assert "https://coreman.example.com/my-wecom" in item.payload["markdown"]
    assert "我已续期" in item.payload["markdown"]
    # 20 小时内不重复提醒；暂停的绑定不提醒。
    assert await reminders.remind(factory, "https://coreman.example.com") == 0
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    row.reminded_at = None
    row.enabled = False
    await db_session.commit()
    assert await reminders.remind(factory, "https://coreman.example.com") == 0
    assert (await db_session.get(Bot, bot.id)).platform == "wecom"
