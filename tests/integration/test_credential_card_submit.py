"""飞书表单提交：防伪、本人、解封提交、失败取消，以及封存副本一定被擦掉。"""

import json
import uuid

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    CredentialRequest,
    InboundEvent,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.personal_credentials import policy, service
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.credential_helpers import BODY, VALUES, cap_for, owner
from tests.integration.worker_helpers import build_ctx

BASE = "https://coreman.example.com"


async def _click(session, cipher, bot, rid, values, *, clicker="owner_pid"):
    action = {
        "task_id": policy.card_task_id(rid),
        "card_type": "credential",
        "sealed": cipher.encrypt(json.dumps(values), policy.sealed_aad(rid)),
    }
    ev = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4()}",
        kind="card_action",
        chat_type="group",
        chat_id="oc_private",
        sender_platform_user_id=clicker,
        sender_open_id="ou_x",
        reply_context={"chat_id": "oc_private", "message_id": "om_form"},
        payload={"card_action": action, "raw": {"event": {"action": {"form_value": {}}}}},
    )
    session.add(ev)
    await session.flush()
    click = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": clicker},
            session_key="oc_private",
            inbound_event_id=ev.id,
            dedupe_key=f"click:{ev.id}",
        ),
    )
    click.status = "running"
    await session.commit()
    return ev, click


async def _prepared(session):
    bot, user, task, cipher = await owner(session)
    opened = await service.open_request(
        session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": "om_form"}
    await session.commit()
    return bot, user, cipher, row


async def _sealed_left(session, ev, click) -> bool:
    ev_row = await session.get(InboundEvent, ev.id, populate_existing=True)
    task_row = await session.get(Task, click.id, populate_existing=True)
    return "sealed" in ev_row.payload["card_action"] or "sealed" in task_row.payload["card_action"]


async def test_owner_submit_saves_resumes_and_wipes(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    ev, click = await _click(db_session, cipher, bot, row.id, VALUES)
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "submitted" and row.resume_task_id is not None
    done = await db_session.get(Task, click.id, populate_existing=True)
    assert done.result == {"card": "saved"}
    assert not await _sealed_left(db_session, ev, click)


async def test_someone_else_cannot_submit_and_copy_is_still_wiped(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    other = User(login_name="other", display_name="别人", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=other.id, platform="feishu", platform_user_id="other_pid"))
    await db_session.commit()
    ev, click = await _click(db_session, cipher, bot, row.id, VALUES, clicker="other_pid")
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "open"
    done = await db_session.get(Task, click.id, populate_existing=True)
    assert done.result == {"card": "not_owner"}
    assert not await _sealed_left(db_session, ev, click)


async def test_invalid_values_cancel_the_request(db_session, db_engine):
    bot, user, cipher, row = await _prepared(db_session)
    ev, click = await _click(db_session, cipher, bot, row.id, {"DEMO_USERNAME": "alice"})
    await CardActionHandler().run(build_ctx(db_engine, click))
    await db_session.refresh(row)
    assert row.status == "cancelled"
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert "提交未成功" in json.dumps(card.payload, ensure_ascii=False)
    assert not await _sealed_left(db_session, ev, click)
