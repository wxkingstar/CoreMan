"""Consent callback binds the sent card, owner, private origin and current selection."""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import FeishuPersonalGrant, InboundEvent, OutboxItem
from coreman.core.feishu_personal import permissions, policy, service
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat.personal import reject_unavailable
from tests.api.test_feishu_personal import setup
from tests.api.test_feishu_personal_worker import intake_for
from tests.integration.worker_helpers import build_ctx


async def prepare(session, app, engine):
    bot, user, original = await setup(session, app)
    intake = await intake_for(session, bot, original, "连接飞书")
    await reject_unavailable(session, build_ctx(engine, original), intake)
    await session.flush()
    sent = (await session.scalars(select(OutboxItem))).one()
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": "om_card"}
    action = {
        "task_id": f"personal:{original.id}",
        "level": "all",
        "event_key": "personal_authorize",
    }
    ev = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="group",
        chat_id=intake.chat_id,
        sender_platform_user_id="human",
        sender_open_id="ou_human",
        reply_context={"chat_id": intake.chat_id, "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-test",
                },
                "event": {
                    "operator": {"user_id": "human", "open_id": "ou_human"},
                    "context": {"open_chat_id": intake.chat_id, "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(ev)
    await session.flush()
    click = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": "human"},
            session_key=intake.chat_id,
            inbound_event_id=ev.id,
            dedupe_key=f"click:{ev.id}",
        ),
    )
    click.status = "running"
    await session.commit()
    return bot, user, original, ev, click, sent


@pytest.mark.parametrize(
    "denial",
    [
        "",
        "other_user",
        "other_chat",
        "other_tenant",
        "other_app",
        "unsent",
        "group_origin",
        "expired",
        "old_card",
        "replay",
    ],
)
async def test_personal_card_binds_verified_owner_and_current_selection(
    db_session, app, db_engine, monkeypatch, denial
):
    bot, user, original, event, click, sent = await prepare(db_session, app, db_engine)
    raw = copy.deepcopy(event.payload)
    if denial == "other_user":
        raw["raw"]["event"]["operator"]["open_id"] = "someone_else"
    elif denial == "other_chat":
        event.chat_id = "other_chat"
    elif denial == "other_tenant":
        raw["raw"]["header"]["tenant_key"] = "other_tenant"
    elif denial == "other_app":
        raw["raw"]["header"]["app_id"] = "other_app"
    elif denial == "unsent":
        sent.status = "pending"
    elif denial == "group_origin":
        origin = await db_session.get(InboundEvent, original.inbound_event_id)
        origin.chat_type = "group"
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    if denial == "expired":
        row.pending_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif denial == "old_card":
        row.pending_enc = app.state.cipher.encrypt("999999", service._aad(row, "pending_enc"))
    elif denial == "replay":
        row.status = "pending"
    event.payload = raw
    await db_session.commit()
    http = AsyncMock(
        return_value={
            "device_code": "secret",
            "verification_uri_complete": "https://accounts.feishu.cn/verify",
            "expires_in": 600,
        }
    )
    monkeypatch.setattr(service, "_http", http)
    monkeypatch.setattr(
        permissions,
        "app_user_scopes",
        AsyncMock(
            return_value=[*permissions.MESSAGE_SCOPES, "im:message", "im:message.send_as_user"]
        ),
    )
    await CardActionHandler().run(build_ctx(db_engine, click))
    if denial:
        http.assert_not_called()
    else:
        http.assert_awaited_once()
        await db_session.refresh(row)
        assert row.status == "pending" and row.authorization_level == "all"
        update = (
            await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        ).one()
        assert update.target["message_id"] == "om_card"
        assert update.payload["card"]["body"]["elements"][1]["text"]["content"] == "前往飞书授权"
    # A card callback can never become a personal-data tool capability.
    with pytest.raises(ValueError, match="private_human_task_required"):
        await policy.task_scope(db_session, click.id, str(user.id))


@pytest.mark.parametrize("level", ["all", "all_except_send", "messages_readonly"])
async def test_card_tier_preserves_authoritative_state_and_rotates_context(
    db_session, app, db_engine, monkeypatch, level
):
    bot, user, original, event, click, sent = await prepare(db_session, app, db_engine)
    # The human-facing replacement for the text prompt must retain the disclosure.
    assert service.RETENTION_NOTICE in str(sent.payload)
    scope = await policy.verified_origin_scope(db_session, original, str(user.id))
    http = AsyncMock(
        return_value={
            "device_code": "secret",
            "verification_uri_complete": "https://accounts.feishu.cn/verify",
            "expires_in": 600,
        }
    )
    monkeypatch.setattr(service, "_http", http)
    state = await service.authorization_status(db_session, app.state.cipher, scope)
    assert state["status"] == "selecting"
    assert state["retention_notice"] == service.RETENTION_NOTICE
    assert state["access_token_expired"] is None
    http.assert_not_called()  # Selecting stores a task id, not device OAuth JSON.
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    selection_epoch = row.context_epoch
    click.payload = {
        **click.payload,
        "card_action": {
            **click.payload["card_action"],
            "level": level,
        },
    }
    monkeypatch.setattr(
        permissions,
        "app_user_scopes",
        AsyncMock(
            return_value=[
                *permissions.MESSAGE_SCOPES,
                "im:message",
                "im:message.send_as_user",
            ]
        ),
    )
    await db_session.commit()
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.authorization_level == level
    assert row.context_epoch != selection_epoch
    assert row.status == "pending" and row.token_enc is None
    assert ("im:message.send_as_user" in row.requested_scopes) == (level == "all")
    assert http.await_count == 1
