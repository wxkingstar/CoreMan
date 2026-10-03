"""发起索取：送达目标、去重、平台条件与令牌所在轮次。"""

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import delete, select

from coreman.core.bus import tasks
from coreman.core.db.models import CredentialRequest, OutboxItem, UserIdentity
from coreman.core.personal_credentials import service
from coreman.core.personal_credentials.policy import CredentialError
from tests.integration.credential_helpers import (
    BODY,
    DEMO_RELAY_SESSION,
    cap_for,
    cron_cap,
    login_app,
    owner,
)

BASE = "https://coreman.example.com"


async def test_feishu_private_form_is_validated_dm_and_deduplicated(db_session):
    bot, user, task, cipher = await owner(db_session)
    opened = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    await db_session.commit()
    assert opened.status == "form_sent"
    row = await db_session.get(CredentialRequest, opened.request_id)
    assert row.status == "open" and row.delivery_chat_id == "oc_private"
    assert row.origin_event_id == task.inbound_event_id
    assert row.origin_session_key == task.session_key
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    assert item.target == {
        "chat_id": "oc_private",
        "recipient_user_id": str(user.id),
        "recipient_platform_user_id": "owner_pid",
    }
    assert item.payload["card"]["task_id"] == f"credential@{row.id}"
    again = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    await db_session.commit()
    assert again.status == "already_pending" and again.request_id == row.id
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 1


async def test_a_pending_form_is_reused_only_by_the_same_origin(db_session):
    bot, user, task, cipher = await owner(db_session)
    cron = cron_cap(bot, user, task)
    chat = cap_for(bot, user, task)
    other_chat = replace(chat, session_key="oc_other")
    opened = [
        await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
        for cap in (cron, chat, other_chat)
    ]
    # 同一个来源再来一次才复用：定时任务按任务，对话按会话。
    again = [
        await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
        for cap in (cron, chat, other_chat)
    ]
    await db_session.commit()
    assert [o.status for o in opened] == ["form_sent"] * 3
    assert len({o.request_id for o in opened}) == 3
    assert [(a.status, a.request_id) for a in again] == [
        ("already_pending", o.request_id) for o in opened
    ]
    # 另一个定时任务也是另一个来源。
    other_job = replace(cron, cron_job_id=uuid.uuid4())
    assert (
        await service.open_request(db_session, cipher, other_job, BODY, base_url=BASE)
    ).status == "form_sent"
    assert len((await db_session.scalars(select(CredentialRequest))).all()) == 4


async def test_request_remembers_the_relay_session_that_asked(db_session):
    bot, user, task, cipher = await owner(db_session)
    chat = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    cron = await service.open_request(
        db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE
    )
    await db_session.commit()
    asked = await db_session.get(CredentialRequest, chat.request_id)
    scheduled = await db_session.get(CredentialRequest, cron.request_id)
    assert asked.origin_relay_session_id == DEMO_RELAY_SESSION
    assert scheduled.origin_relay_session_id is None


async def test_a_form_asked_before_a_reset_is_not_reused_by_the_new_conversation(db_session):
    bot, user, task, cipher = await owner(db_session)
    before = cap_for(bot, user, task)
    after = cap_for(bot, user, task, relay_session_id=uuid.uuid4())
    first = await service.open_request(db_session, cipher, before, BODY, base_url=BASE)
    second = await service.open_request(db_session, cipher, after, BODY, base_url=BASE)
    same = await service.open_request(db_session, cipher, after, BODY, base_url=BASE)
    await db_session.commit()
    assert (first.status, second.status) == ("form_sent", "form_sent")
    assert second.request_id != first.request_id
    assert (same.status, same.request_id) == ("already_pending", second.request_id)
    assert len((await db_session.scalars(select(CredentialRequest))).all()) == 2


async def test_dm_record_without_platform_identity_is_not_a_private_delivery(db_session):
    bot, user, task, cipher = await owner(db_session)
    cap = cap_for(bot, user, task)
    await db_session.execute(delete(UserIdentity).where(UserIdentity.user_id == user.id))
    await db_session.commit()
    # 定时任务只能靠私聊送达，没有本人身份就送不了。
    with pytest.raises(CredentialError) as exc:
        await service.open_request(
            db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE
        )
    assert exc.value.code == "unreachable"
    await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
    await db_session.commit()
    [item] = (await db_session.scalars(select(OutboxItem))).all()
    assert item.target == {"chat_id": cap.chat_id}


async def test_group_origin_goes_to_dm_with_group_notice(db_session):
    bot, user, task, cipher = await owner(db_session, chat_type="group")
    await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    items = {i.target["chat_id"]: i for i in (await db_session.scalars(select(OutboxItem))).all()}
    assert "card" in items["oc_private"].payload
    assert items["oc_group"].payload == {"markdown": "已私信你一张安全表单，请在私聊里填写。"}


async def test_group_origin_without_dm_record_posts_card_in_group_with_mention(db_session):
    bot, user, task, cipher = await owner(db_session, chat_type="group", reached=False)
    await service.open_request(db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE)
    await db_session.commit()
    [item] = (await db_session.scalars(select(OutboxItem))).all()
    assert item.target == {"chat_id": "oc_group"}
    assert "<at id=ou_owner></at>" in json.dumps(item.payload, ensure_ascii=False)


async def test_feishu_web_link_only_with_login_app(db_session):
    bot, user, task, cipher = await owner(db_session)
    await login_app(db_session, "feishu")
    opened = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    await db_session.commit()
    item = await db_session.scalar(select(OutboxItem))
    assert f"{BASE}/my-credentials/requests/{opened.request_id}" in json.dumps(item.payload)


async def test_wecom_needs_login_app_and_sends_link(db_session):
    bot, user, task, cipher = await owner(db_session, platform="wecom")
    # rollback 会让所有 ORM 实例过期，异步下再访问属性会抛 MissingGreenlet，所以令牌先取一次。
    cap = cap_for(bot, user, task)
    with pytest.raises(CredentialError) as exc:
        await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
    assert exc.value.code == "login_unavailable"
    await db_session.rollback()
    await login_app(db_session, "wecom")
    opened = await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
    await db_session.commit()
    item = await db_session.scalar(select(OutboxItem))
    assert f"]({BASE}/my-credentials/requests/{opened.request_id})" in item.payload["markdown"]


async def test_cron_origin_without_dm_record_is_unreachable(db_session):
    bot, user, task, cipher = await owner(db_session, reached=False)
    with pytest.raises(CredentialError) as exc:
        await service.open_request(
            db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE
        )
    assert exc.value.code == "unreachable"


async def test_capability_dies_with_its_turn_and_fields_are_checked(db_session):
    bot, user, task, cipher = await owner(db_session)
    cap = cap_for(bot, user, task)
    with pytest.raises(CredentialError) as bad:
        await service.open_request(
            db_session,
            cipher,
            cap,
            {"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"},
            base_url=BASE,
        )
    assert bad.value.code == "invalid_fields"
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    with pytest.raises(CredentialError) as ended:
        await service.open_request(db_session, cipher, cap, BODY, base_url=BASE)
    assert ended.value.code == "inactive"
