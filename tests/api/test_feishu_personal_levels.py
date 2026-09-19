"""Chosen authorization is a durable ceiling, never inferred by the model."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from coreman.core.db.models import FeishuPersonalGrant
from coreman.core.feishu_personal import permissions, policy, service
from coreman.runtime.worker.chat.personal import intercept
from tests.api.test_feishu_personal import URL, grant, headers, rpc, setup, value
from tests.api.test_feishu_personal_worker import intake_for
from tests.integration.worker_helpers import build_ctx


async def test_agent_cannot_choose_or_start_authorization(client, app, db_session):
    _, user, task = await setup(db_session, app)
    result = value(
        await client.post(URL, headers=await headers(app, task, user), json=rpc("feishu_authorize"))
    )
    assert result["status"] == "selection_required"
    result = value(
        await client.post(
            URL,
            headers=await headers(app, task, user),
            json=rpc("feishu_authorize", {"level": "all"}),
        )
    )
    assert result["error"] == "invalid_tool_or_arguments"


async def test_connection_shows_choices_without_contacting_oauth(
    db_session, app, db_engine, monkeypatch
):
    bot, user, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "连接飞书")
    http = AsyncMock()
    monkeypatch.setattr(service, "_http", http)
    assert await intercept(db_session, build_ctx(db_engine, task), intake)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert row.status == "selecting" and row.pending_enc is not None
    assert row.selection_chat_id == intake.chat_id
    state = await service.authorization_status(
        db_session,
        app.state.cipher,
        await policy.verified_origin_scope(db_session, task, str(user.id)),
    )
    assert state["status"] == "selecting"
    http.assert_not_called()


@pytest.mark.parametrize(
    "choice,expected", [("1", "all"), ("2", "all_except_send"), ("3", "messages_readonly")]
)
async def test_chosen_level_controls_requested_scopes(
    db_session, app, monkeypatch, choice, expected
):
    _, user, task = await setup(db_session, app)
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    await service.begin_selection(db_session, app.state.cipher, scope)
    available = list(permissions.MESSAGE_SCOPES) + [
        "docx:document:write_only",
        "im:message",
        "im:message.send_as_user",
    ]
    monkeypatch.setattr(permissions, "app_user_scopes", AsyncMock(return_value=available))
    http = AsyncMock(
        return_value={
            "device_code": "secret",
            "verification_uri_complete": "https://accounts.feishu.cn/verify",
            "expires_in": 600,
        }
    )
    monkeypatch.setattr(service, "_http", http)
    result = await service.choose_authorization(db_session, app.state.cipher, scope, choice)
    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    assert row.authorization_level == expected and result["status"] == "pending"
    requested = set(http.call_args.kwargs["data"]["scope"].split())
    assert ("docx:document:write_only" in requested) == (choice != "3")
    assert ("im:message.send_as_user" in requested) == (choice == "1")


async def test_missing_send_permission_never_looks_like_complete_third_tier(
    db_session, app, monkeypatch
):
    _, user, task = await setup(db_session, app)
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    await service.begin_selection(db_session, app.state.cipher, scope)
    monkeypatch.setattr(
        permissions, "app_user_scopes", AsyncMock(return_value=list(permissions.MESSAGE_SCOPES))
    )
    with pytest.raises(service.PersonalError, match="app_send_permission_missing"):
        await service.choose_authorization(db_session, app.state.cipher, scope, "1")


async def test_historical_broad_grant_cannot_expand_readonly_selection(
    db_session, app, monkeypatch
):
    bot, user, task = await setup(db_session, app)
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="messages_readonly",
        scopes=["docx:document:readonly", "im:message"],
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    http = AsyncMock()
    monkeypatch.setattr(service, "_http", http)
    with pytest.raises(service.PersonalError, match="outside_selected_authorization"):
        await service.api_request(
            db_session, app.state.cipher, scope, "GET", "/docx/v1/documents/doc1/raw_content"
        )
    http.assert_not_called()


async def test_reconnect_stops_old_access_even_if_remote_revoke_fails(db_session, app, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="all")
    monkeypatch.setattr(service.revocation, "revoke_tokens", AsyncMock(return_value=False))
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    result = await service.begin_selection(db_session, app.state.cipher, scope)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert result["remote_revoked"] is False and row.token_enc is None
    assert row.status == "selecting"
    with pytest.raises(service.PersonalError, match="authorization_required"):
        await service.api_request(db_session, app.state.cipher, scope, "GET", "/im/v1/messages")


async def test_expired_choice_cannot_create_link(db_session, app):
    _, user, task = await setup(db_session, app)
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    await service.begin_selection(db_session, app.state.cipher, scope)
    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    row.pending_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(service.PersonalError, match="selection_required"):
        await service.choose_authorization(db_session, app.state.cipher, scope, "1")


@pytest.mark.parametrize("level", ["legacy_readonly", "messages_readonly", "all_except_send"])
async def test_send_denied_despite_historical_send_grant(db_session, app, monkeypatch, level):
    bot, user, task = await setup(db_session, app)
    scopes = ["im:message", "im:message.send_as_user"]
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level=level,
        scopes=scopes,
        requested_scopes=scopes,
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    http = AsyncMock()
    monkeypatch.setattr(service, "_http", http)
    with pytest.raises(service.PersonalError, match="sending_not_authorized"):
        await service.api_request(db_session, app.state.cipher, scope, "POST", "/im/v1/messages")
    http.assert_not_called()


async def test_send_requires_actual_and_requested_scope(db_session, app, monkeypatch):
    bot, user, task = await setup(db_session, app)
    scopes = ["im:message", "im:message.send_as_user"]
    row = await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=scopes,
        requested_scopes=scopes,
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    http = AsyncMock(return_value={"code": 0, "data": {"message_id": "om_test"}})
    monkeypatch.setattr(service, "_http", http)
    assert await service.api_request(
        db_session, app.state.cipher, scope, "POST", "/im/v1/messages"
    ) == {"message_id": "om_test"}
    row.scopes = ["im:message"]
    await db_session.flush()
    with pytest.raises(service.PersonalError, match="sending_not_authorized"):
        await service.api_request(db_session, app.state.cipher, scope, "POST", "/im/v1/messages")
    assert http.call_count == 1


async def test_expired_selection_does_not_trap_ordinary_chat(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    await service.begin_selection(db_session, app.state.cipher, scope)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    row.pending_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    intake = await intake_for(db_session, bot, task, "普通问题")
    assert not await intercept(db_session, build_ctx(db_engine, task), intake)


async def test_repeated_revoke_does_not_misreport_remote_success(db_session, app, monkeypatch):
    bot, user, _ = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    monkeypatch.setattr(service.revocation, "revoke_tokens", AsyncMock(return_value=False))
    first = await service.revoke_grant(db_session, bot.id, user.id, app.state.cipher)
    await db_session.flush()
    second = await service.revoke_grant(db_session, bot.id, user.id, app.state.cipher)
    assert not first["remote_revoked"] and not second["remote_revoked"]


async def test_plain_digit_without_selection_does_not_start_authorization(
    db_session, app, db_engine
):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "3")
    assert not await intercept(db_session, build_ctx(db_engine, task), intake)


async def test_unrequested_historical_document_permission_is_blocked(db_session, app, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all_except_send",
        scopes=["docx:document:readonly"],
        requested_scopes=["search:message"],
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    monkeypatch.setattr(service, "_http", AsyncMock())
    with pytest.raises(service.PersonalError, match="selected_permission_missing"):
        await service.api_request(
            db_session, app.state.cipher, scope, "GET", "/docx/v1/documents/doc1/raw_content"
        )
    service._http.assert_not_called()


EVENTS = "/calendar/v4/calendars/cal1/events"


@pytest.mark.parametrize(
    "level,expected",
    [("messages_readonly", "outside_selected_authorization"), ("all_except_send", None)],
)
async def test_writes_need_a_tier_that_allows_them(db_session, app, monkeypatch, level, expected):
    bot, user, task = await setup(db_session, app)
    scopes = ["calendar:calendar.event:create", "mail:user_mailbox.message:send"]
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level=level,
        scopes=scopes,
        requested_scopes=scopes,
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    http = AsyncMock(return_value={"code": 0, "data": {"event": {"event_id": "ev1"}}})
    monkeypatch.setattr(service, "_http", http)
    if expected:
        with pytest.raises(service.PersonalError, match=expected):
            await service.api_request(db_session, app.state.cipher, scope, "POST", EVENTS)
    else:
        out = await service.api_request(db_session, app.state.cipher, scope, "POST", EVENTS)
        assert out == {"event": {"event_id": "ev1"}}
    # Sending mail is a send even though the app granted it: the middle tier refuses it.
    with pytest.raises(service.PersonalError, match="sending_not_authorized"):
        await service.api_request(
            db_session,
            app.state.cipher,
            scope,
            "POST",
            "/mail/v1/user_mailboxes/me/drafts/d1/send",
        )
    assert http.call_count == (0 if expected else 1)


async def test_unregistered_paths_never_reach_feishu(db_session, app, monkeypatch):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, authorization_level="all")
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    http = AsyncMock()
    monkeypatch.setattr(service, "_http", http)
    for method, path in [
        ("GET", "/contact/v3/users"),
        ("DELETE", "/drive/v1/files/f1"),
        ("GET", "/calendar/v4/calendars/../events/instance_view"),
    ]:
        with pytest.raises(service.PersonalError, match="invalid_tool_or_arguments"):
            await service.api_request(db_session, app.state.cipher, scope, method, path)
    http.assert_not_called()


@pytest.mark.parametrize(
    "code,expected,upstream_code",
    [
        (99991672, "app_permission_missing", None),
        (99991679, "user_permission_missing", None),
        (190004, "feishu_request_failed", 190004),
    ],
)
async def test_upstream_refusals_name_what_to_fix(
    db_session, app, monkeypatch, code, expected, upstream_code
):
    bot, user, task = await setup(db_session, app)
    scopes = ["calendar:calendar.event:create"]
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=scopes,
        requested_scopes=scopes,
    )
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    monkeypatch.setattr(
        service, "_http", AsyncMock(return_value={"code": code, "msg": "user-secret leaked"})
    )
    with pytest.raises(service.PersonalError) as caught:
        await service.api_request(db_session, app.state.cipher, scope, "POST", EVENTS)
    assert caught.value.payload() == (
        {"error": expected, "upstream_code": upstream_code}
        if upstream_code
        else {"error": expected}
    )
    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    assert row.status == "connected"


async def test_mail_retries_through_mcp_send_one_mail(db_session, app, client, monkeypatch):
    from coreman.core.db.models import FeishuPersonalSend

    bot, user, task = await setup(db_session, app)
    scopes = [
        "mail:user_mailbox:readonly",
        "mail:user_mailbox.message:modify",
        "mail:user_mailbox.message:send",
    ]
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=scopes,
        requested_scopes=scopes,
    )
    sent = []

    async def feishu(method, url, **kwargs):
        if url.endswith("/profile"):
            return {"code": 0, "data": {"primary_email_address": "me@example.com"}}
        if url.endswith("/drafts"):
            return {"code": 0, "data": {"draft_id": f"d{len(sent) + 1}"}}
        sent.append(url)
        # The first send is lost upstream; later ones go through.
        return {"code": 0, "data": {"message_id": "m1"}} if len(sent) > 1 else {"code": 1}

    monkeypatch.setattr(service, "_http", AsyncMock(side_effect=feishu))
    args = {"to": ["a@example.com"], "subject": "周报", "body": "内容", "uuid": "report-1"}
    auth = await headers(app, task, user)
    first = value(await client.post(URL, headers=auth, json=rpc("feishu_mail_send", args)))
    assert first == {"error": "feishu_request_failed", "upstream_code": 1}
    second = value(await client.post(URL, headers=auth, json=rpc("feishu_mail_send", args)))
    third = value(await client.post(URL, headers=auth, json=rpc("feishu_mail_send", args)))
    assert second["sent"] and third["repeat"] and third["message_id"] == "m1"
    # Both sends used the draft made first; the third call reached no Feishu API.
    assert [url.rsplit("/", 2)[-2] for url in sent] == ["d1", "d1"]
    changed = value(
        await client.post(
            URL, headers=auth, json=rpc("feishu_mail_send", {**args, "body": "别的内容"})
        )
    )
    assert changed == {"error": "uuid_reused"}
    row = (await db_session.scalars(select(FeishuPersonalSend))).one()
    assert (row.status, row.draft_id, row.send_key) == ("sent", "d1", "report-1")
