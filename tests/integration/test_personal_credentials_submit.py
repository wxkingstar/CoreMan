"""提交：本人、状态、值校验、加密写入、审计、续接与卡片更新；以及取消、过期、清理。"""

import json
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from coreman.core.db.models import (
    AuditLog,
    ChatSession,
    CredentialRequest,
    OutboxItem,
    Task,
    User,
    UserReached,
)
from coreman.core.personal_credentials import service, store
from coreman.core.personal_credentials.policy import CredentialError
from coreman.core.timeutils import utcnow
from tests.integration.credential_helpers import (
    BODY,
    VALUES,
    cap_for,
    cron_cap,
    login_app,
    owner,
    seed_chat_session,
)
from tests.integration.test_chat_handler import chat_task

BASE = "https://coreman.example.com"


async def _opened(session, **kw):
    bot, user, task, cipher = await owner(session, **kw)
    if kw.get("platform") == "wecom":
        await login_app(session, "wecom")
    await seed_chat_session(session, bot, task)
    opened = await service.open_request(
        session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": "om_form"}
    await session.commit()
    return bot, user, task, cipher, row


async def _dump_everything(session) -> str:
    tasks_ = [t.payload for t in (await session.scalars(select(Task))).all()]
    items = [(i.target, i.payload) for i in (await session.scalars(select(OutboxItem))).all()]
    audits = [a.diff for a in (await session.scalars(select(AuditLog))).all()]
    return json.dumps([tasks_, items, audits], ensure_ascii=False, default=str)


async def test_saved_values_resume_and_card_update_without_plaintext(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    assert result.status == "saved" and result.keys == ("DEMO_PIN", "DEMO_USERNAME")
    await db_session.refresh(row)
    assert row.status == "submitted" and row.resume_task_id is not None
    resume = await db_session.get(Task, row.resume_task_id)
    assert resume.kind == "credential_resume" and resume.user_id == user.id
    assert (
        resume.session_key == task.session_key and resume.inbound_event_id == task.inbound_event_id
    )
    assert resume.payload["credential_request_id"] == str(row.id)
    assert resume.payload["serialize_session"] is True
    update_item = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update")
    )
    assert update_item.target["message_id"] == "om_form"
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "personal_credential.saved")
    )
    assert audit.diff == {"keys": ["DEMO_PIN", "DEMO_USERNAME"]}
    dumped = await _dump_everything(db_session)
    assert "pin-778899" not in dumped and "alice" not in dumped
    found = await store.injected(db_session, cipher, bot_id=bot.id, user_id=user.id)
    assert found.env == VALUES


async def test_owner_status_and_value_rules(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    stranger = User(login_name="stranger", display_name="别人", source="sync")
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(CredentialError) as exc:
        await service.submit(db_session, cipher, row.id, actor_id=stranger.id, values=VALUES)
    assert exc.value.code == "forbidden"
    invalid = await service.submit(
        db_session, cipher, row.id, actor_id=user.id, values={"DEMO_USERNAME": "alice"}
    )
    assert invalid.status == "invalid" and "PIN" in invalid.message
    await db_session.refresh(row)
    assert row.status == "open"
    assert (
        await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    ).status == "saved"
    await db_session.commit()
    assert (
        await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    ).status == "duplicate"


async def test_expired_request_is_closed_with_expired_card(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    await db_session.execute(
        update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(row)
    assert result.status == "expired" and row.status == "expired"
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    assert "已过期" in json.dumps(card.payload, ensure_ascii=False)


async def test_cancel_and_expire_due_and_cleanup(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    await service.cancel(db_session, row.id, "「PIN」不能为空")
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "cancelled"
    second = await service.open_request(
        db_session, cipher, cap_for(bot, user, task), BODY, base_url=BASE
    )
    assert second.status == "form_sent"
    await db_session.execute(
        update(CredentialRequest)
        .where(CredentialRequest.id == second.request_id)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await service.expire_due(db_session, utcnow()) == 1
    await db_session.commit()
    await db_session.execute(
        update(CredentialRequest).values(created_at=utcnow() - timedelta(days=91))
    )
    await db_session.commit()
    assert await service.cleanup(db_session, utcnow()) == 2
    await db_session.commit()


async def test_cron_origin_on_wecom_says_saved_instead_of_resuming(db_session):
    bot, user, task, cipher = await owner(db_session, platform="wecom")
    await login_app(db_session, "wecom")
    opened = await service.open_request(
        db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE
    )
    await db_session.commit()
    result = await service.submit(
        db_session, cipher, opened.request_id, actor_id=user.id, values=VALUES
    )
    await db_session.commit()
    row = await db_session.get(CredentialRequest, opened.request_id)
    assert result.status == "saved" and row.resume_task_id is None
    said = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"credential-request:{row.id}:saved")
    )
    assert said.target == {
        "chat_id": "oc_private",
        "recipient_user_id": str(user.id),
        "recipient_platform_user_id": "owner_pid",
    }
    assert "下次执行时生效" in said.payload["markdown"]


async def test_saved_notice_is_bare_when_delivery_chat_is_no_longer_the_private_chat(db_session):
    bot, user, task, cipher = await owner(db_session, platform="wecom")
    await login_app(db_session, "wecom")
    opened = await service.open_request(
        db_session, cipher, cron_cap(bot, user, task), BODY, base_url=BASE
    )
    await db_session.execute(update(UserReached).values(platform_chat_id="oc_other"))
    await db_session.commit()
    await service.submit(db_session, cipher, opened.request_id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    said = await db_session.scalar(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"credential-request:{opened.request_id}:saved"
        )
    )
    assert said.target == {"chat_id": "oc_private"}


async def _waiting(session, cipher, cap, body=BODY):
    """发一张表单，并让它的飞书卡片带上消息 ID（提交时才会换成结果卡）。"""
    opened = await service.open_request(session, cipher, cap, body, base_url=BASE)
    row = await session.get(CredentialRequest, opened.request_id)
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": f"om_{row.id}"}
    await session.commit()
    return row


async def _resumes(session) -> dict[str, Task]:
    found = await session.scalars(select(Task).where(Task.kind == "credential_resume"))
    return {t.payload["credential_request_id"]: t for t in found}


async def test_filling_a_cron_form_also_settles_the_chat_form_waiting_on_the_same_keys(db_session):
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    cron = await _waiting(db_session, cipher, cron_cap(bot, user, task))
    chat = await _waiting(db_session, cipher, cap_for(bot, user, task))
    assert cron.id != chat.id
    result = await service.submit(db_session, cipher, cron.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    assert result.status == "saved" and "下次执行时生效" in result.message
    await db_session.refresh(cron)
    await db_session.refresh(chat)
    assert (cron.status, chat.status) == ("submitted", "submitted")
    assert chat.submitted_at is not None and cron.resume_task_id is None
    resumes = await _resumes(db_session)
    assert list(resumes) == [str(chat.id)] and chat.resume_task_id == resumes[str(chat.id)].id
    assert resumes[str(chat.id)].session_key == task.session_key
    cards = {
        i.target["message_id"]: json.dumps(i.payload, ensure_ascii=False)
        for i in (
            await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        )
    }
    assert set(cards) == {f"om_{cron.id}", f"om_{chat.id}"}
    assert "AI 员工会继续之前的任务" in cards[f"om_{chat.id}"]
    assert "下次执行时生效" in cards[f"om_{cron.id}"]
    # 值只写一次，也只记一条审计。
    audits = await db_session.scalars(
        select(AuditLog).where(AuditLog.action == "personal_credential.saved")
    )
    assert len(audits.all()) == 1
    assert "pin-778899" not in await _dump_everything(db_session)


async def test_filling_one_form_resumes_every_chat_session_waiting_on_the_keys(db_session):
    bot, user, task, cipher = await owner(db_session)
    group_task = await chat_task(
        db_session, bot, "再查一下", sender="owner_pid", chat_type="group", chat_id="oc_group"
    )
    await seed_chat_session(db_session, bot, task)
    await seed_chat_session(db_session, bot, group_task)
    first = await _waiting(db_session, cipher, cap_for(bot, user, task))
    second = await _waiting(db_session, cipher, cap_for(bot, user, group_task))
    assert first.id != second.id
    await service.submit(db_session, cipher, first.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(second)
    assert second.status == "submitted"
    resumes = await _resumes(db_session)
    assert set(resumes) == {str(first.id), str(second.id)}
    assert resumes[str(first.id)].session_key == task.session_key
    assert resumes[str(second.id)].session_key == group_task.session_key
    assert resumes[str(second.id)].inbound_event_id == group_task.inbound_event_id
    # 已经结算的那张表单之后再填，只会得到「已提交」。
    again = await service.submit(db_session, cipher, second.id, actor_id=user.id, values=VALUES)
    assert again.status == "duplicate"


async def test_only_unexpired_forms_whose_keys_are_all_covered_are_settled(db_session):
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    chat = cap_for(bot, user, task)
    wider = {
        **BODY,
        "fields": [*BODY["fields"], {"key": "DEMO_EXTRA", "label": "额外", "secret": True}],
    }
    main = await _waiting(db_session, cipher, chat)
    extra_key = await _waiting(db_session, cipher, replace(chat, session_key="oc_a"), wider)
    stale = await _waiting(db_session, cipher, replace(chat, session_key="oc_b"))
    stale.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    await service.submit(db_session, cipher, main.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(extra_key)
    await db_session.refresh(stale)
    assert (extra_key.status, stale.status) == ("open", "open")
    assert extra_key.resume_task_id is None and stale.resume_task_id is None
    assert list(await _resumes(db_session)) == [str(main.id)]


async def _saved_without_resume(session, cipher, bot, user, row, platform):
    result = await service.submit(session, cipher, row.id, actor_id=user.id, values=VALUES)
    await session.commit()
    await session.refresh(row)
    assert result.status == "saved" and "下次对话时生效" in result.message
    assert row.status == "submitted" and row.resume_task_id is None
    assert await _resumes(session) == {}
    if platform == "wecom":
        said = await session.scalar(
            select(OutboxItem).where(OutboxItem.dedupe_key == f"credential-request:{row.id}:saved")
        )
        assert "下次对话时生效" in said.payload["markdown"]
    else:
        card = await session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        text = json.dumps(card.payload, ensure_ascii=False)
        assert "下次对话时生效" in text and "继续之前的任务" not in text


@pytest.mark.parametrize("platform", ["feishu", "wecom"])
async def test_no_resume_is_promised_after_the_session_was_reset(db_session, platform):
    bot, user, task, cipher, row = await _opened(db_session, platform=platform)
    chat_session = await db_session.get(ChatSession, (bot.id, task.session_key))
    chat_session.relay_session_id = uuid.uuid4()
    await db_session.commit()
    await _saved_without_resume(db_session, cipher, bot, user, row, platform)


async def test_no_resume_is_promised_after_the_session_was_cleared(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    await db_session.execute(delete(ChatSession).where(ChatSession.bot_id == bot.id))
    await db_session.commit()
    await _saved_without_resume(db_session, cipher, bot, user, row, "feishu")


async def test_no_resume_is_promised_for_a_request_that_recorded_no_session(db_session):
    bot, user, task, cipher, row = await _opened(db_session)
    row.origin_relay_session_id = None
    await db_session.commit()
    await _saved_without_resume(db_session, cipher, bot, user, row, "feishu")


async def test_a_stale_form_from_before_a_reset_does_not_resume_next_to_the_new_one(db_session):
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    stale = await _waiting(db_session, cipher, cap_for(bot, user, task))
    # 用户重置对话，新对话为同一组变量又发了一张表单。
    renewed = uuid.uuid4()
    chat_session = await db_session.get(ChatSession, (bot.id, task.session_key))
    chat_session.relay_session_id = renewed
    await db_session.commit()
    current = await _waiting(db_session, cipher, cap_for(bot, user, task, relay_session_id=renewed))
    assert stale.id != current.id
    await service.submit(db_session, cipher, current.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(stale)
    await db_session.refresh(current)
    assert (stale.status, current.status) == ("submitted", "submitted")
    # 只有新对话那张续接；旧表单不排任务，也就不会再冒出一句「对话已重置，请重新发起」。
    resumes = await _resumes(db_session)
    assert list(resumes) == [str(current.id)] and stale.resume_task_id is None
    cards = {
        i.target["message_id"]: json.dumps(i.payload, ensure_ascii=False)
        for i in (
            await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        )
    }
    assert "AI 员工会继续之前的任务" in cards[f"om_{current.id}"]
    assert "下次对话时生效" in cards[f"om_{stale.id}"]
    assert "继续之前的任务" not in cards[f"om_{stale.id}"]
    assert "对话已重置" not in await _dump_everything(db_session)


async def test_one_conversation_is_resumed_once_however_many_forms_it_has_open(
    db_session, monkeypatch
):
    bot, user, task, cipher = await owner(db_session)
    await seed_chat_session(db_session, bot, task)
    cap = cap_for(bot, user, task)
    main = await _waiting(db_session, cipher, cap)
    # 同一对话同时发出的两张表单（并发或重试各发了一张）：绕过去重直接造出第二张。
    with monkeypatch.context() as patched:
        patched.setattr(service, "_same_origin", lambda row, cap: False)
        twin = await _waiting(db_session, cipher, cap)
    # 同一对话里另一张键被覆盖的表单。
    narrower = await _waiting(db_session, cipher, cap, {**BODY, "fields": BODY["fields"][1:]})
    assert len({main.id, twin.id, narrower.id}) == 3
    result = await service.submit(db_session, cipher, main.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    assert result.status == "saved" and "AI 员工会继续之前的任务" in result.message
    for row in (main, twin, narrower):
        await db_session.refresh(row)
        assert row.status == "submitted"
    resumes = await _resumes(db_session)
    assert list(resumes) == [str(main.id)] and main.resume_task_id == resumes[str(main.id)].id
    assert twin.resume_task_id is None and narrower.resume_task_id is None
    # 对话只续接一次，所以其余表单的结果卡也都说「会继续」。
    cards = {
        i.target["message_id"]: json.dumps(i.payload, ensure_ascii=False)
        for i in (
            await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
        )
    }
    assert set(cards) == {f"om_{main.id}", f"om_{twin.id}", f"om_{narrower.id}"}
    assert all("AI 员工会继续之前的任务" in card for card in cards.values())
