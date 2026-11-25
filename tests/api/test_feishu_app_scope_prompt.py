"""Admins chatting privately with their own AI employee get a card to top up missing app scopes."""

import json
from types import SimpleNamespace

from sqlalchemy import select

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.db.models import AuditLog, BotMember, FeishuAppRegistration, OutboxItem
from coreman.core.feishu_apps import registration, scope_prompt
from coreman.runtime.worker.chat.handler import ChatTaskHandler
from tests.api.test_feishu_app_registrations import URL, FakeFeishu
from tests.api.test_feishu_personal import setup

MISSING = ["docs:document.media:download", "im:message.reactions:read"]


def _app_lacks(monkeypatch, missing):
    calls = []

    async def fetch(app_id, secret, *, observe):
        calls.append(app_id)
        return list(missing)

    monkeypatch.setattr(scope_prompt, "fetch_missing", fetch)
    return calls


def _fake_scan(monkeypatch) -> FakeFeishu:
    fake = FakeFeishu()
    monkeypatch.setattr(registration, "begin", fake.begin)
    monkeypatch.setattr(registration, "poll", fake.poll)
    return fake


async def _admin_setup(db_session, app):
    bot, user, task = await setup(db_session, app)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    await db_session.commit()
    return bot, user, task


async def _outbox(db_session, kind):
    rows = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.kind == kind).order_by(OutboxItem.id)
    )
    return list(rows)


def _click(n=1, sender="human"):
    return SimpleNamespace(
        id=n,
        chat_id="oc_private",
        reply_context={"message_id": "om_card"},
        sender_platform_user_id=sender,
        sender_open_id="ou_" + sender,
        payload={"raw": {"header": {"tenant_key": "tenant-test"}}},
    )


def _action(task):
    return {"task_id": f"personal:{task.id}", "event_key": scope_prompt.EVENT_KEY}


async def test_an_admin_gets_one_card_a_day_for_the_same_gap(app, db_session, monkeypatch):
    bot, user, task = await _admin_setup(db_session, app)
    calls = _app_lacks(monkeypatch, MISSING)
    cipher = app.state.cipher

    assert await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)
    assert not await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)
    await db_session.commit()

    [sent] = await _outbox(db_session, "send")
    card = sent.payload["card"]
    assert sent.target == {"chat_id": "oc_private"} and card["task_id"] == f"personal:{task.id}"
    text = json.dumps(card, ensure_ascii=False)
    assert "还缺 2 项权限" in text and "docs:document.media:download" in text
    assert scope_prompt.EVENT_KEY in text
    assert calls == ["cli_test"]  # the second message reused the cached answer


async def test_a_new_gap_is_offered_again_the_same_day(app, db_session, monkeypatch):
    bot, user, task = await _admin_setup(db_session, app)
    cipher = app.state.cipher
    _app_lacks(monkeypatch, MISSING[:1])
    assert await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)
    scope_prompt._checked.clear()
    _app_lacks(monkeypatch, MISSING)
    assert await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)


async def test_no_card_for_strangers_or_a_complete_app(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    cipher = app.state.cipher
    calls = _app_lacks(monkeypatch, MISSING)
    assert not await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)
    assert calls == []  # not an admin: never asks Feishu

    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    await db_session.flush()
    _app_lacks(monkeypatch, [])
    assert not await scope_prompt.offer(db_session, cipher, bot, user.id, "oc_private", task.id)
    assert await _outbox(db_session, "send") == []


async def test_a_private_chat_turn_offers_the_card_only_to_admins(app, db_session, monkeypatch):
    bot, user, task = await setup(db_session, app)
    _app_lacks(monkeypatch, MISSING)
    logs = []
    ctx = SimpleNamespace(
        task=SimpleNamespace(id=task.id, payload={}),
        session_factory=app.state.session_factory,
        cipher=app.state.cipher,
        log=SimpleNamespace(
            info=lambda event, **kw: logs.append(event),
            warning=lambda event, **kw: logs.append(event),
        ),
    )

    def intake(chat_type="single"):
        return SimpleNamespace(
            bot=bot,
            chat_type=chat_type,
            chat_id="oc_private",
            speaker=SimpleNamespace(user_id=user.id),
        )

    handler = ChatTaskHandler()
    await handler._offer_app_scopes(ctx, intake())
    assert logs == []

    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    await db_session.commit()
    await handler._offer_app_scopes(ctx, intake("group"))
    ctx.task.payload = {"origin": "observe"}
    await handler._offer_app_scopes(ctx, intake())
    assert logs == []

    ctx.task.payload = {}
    await handler._offer_app_scopes(ctx, intake())
    assert logs == ["feishu_app_scopes_offered"]
    assert len(await _outbox(db_session, "send")) == 1


async def test_clicking_starts_a_scan_and_shows_the_feishu_link(app, db_session, monkeypatch):
    bot, user, task = await _admin_setup(db_session, app)
    _app_lacks(monkeypatch, MISSING)
    fake = _fake_scan(monkeypatch)
    cipher = app.state.cipher

    out = await scope_prompt.handle(
        db_session, cipher, bot, _click(sender="stranger"), _action(task)
    )
    assert out == "ignored" and fake.begins == []

    out = await scope_prompt.handle(db_session, cipher, bot, _click(), _action(task))
    await db_session.commit()
    assert out == "feishu_app_scopes_started"
    assert fake.begins[0]["app_id"] == "cli_test"
    row = (await db_session.scalars(select(FeishuAppRegistration))).one()
    assert row.purpose == "update" and row.user_id == user.id and row.bot_id == bot.id
    assert row.notify_card == {
        "chat_id": "oc_private",
        "message_id": "om_card",
        "task_id": f"personal:{task.id}",
    }
    [update] = await _outbox(db_session, "card_update")
    assert update.target == {"chat_id": "oc_private", "message_id": "om_card"}
    assert URL in json.dumps(update.payload["card"], ensure_ascii=False)
    audit = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "feishu_app.registration_start")
        )
    ).one()
    assert audit.actor_id == user.id


async def test_clicking_after_someone_else_fixed_it_starts_nothing(app, db_session, monkeypatch):
    bot, _, task = await _admin_setup(db_session, app)
    _app_lacks(monkeypatch, [])
    fake = _fake_scan(monkeypatch)
    out = await scope_prompt.handle(db_session, app.state.cipher, bot, _click(), _action(task))
    assert out == "feishu_app_scopes_complete" and fake.begins == []
    [update] = await _outbox(db_session, "card_update")
    assert "已经补齐" in json.dumps(update.payload["card"], ensure_ascii=False)


async def _started(db_session, app, monkeypatch):
    bot, user, task = await _admin_setup(db_session, app)
    _app_lacks(monkeypatch, MISSING)
    fake = _fake_scan(monkeypatch)
    await scope_prompt.handle(db_session, app.state.cipher, bot, _click(), _action(task))
    row = (await db_session.scalars(select(FeishuAppRegistration))).one()
    row.next_poll_at = None
    await db_session.commit()
    return bot, user, task, fake, row


async def test_the_scheduler_reports_success_on_the_card(app, db_session, monkeypatch):
    bot, user, _, fake, row = await _started(db_session, app, monkeypatch)
    fake.results.append(registration.Poll("succeeded", "cli_test", "rotated-secret", "ou_x"))

    counts = await scope_prompt.sweep(app.state.session_factory, app.state.cipher)
    assert counts == {"feishu_app_scopes_polled": 1}
    await db_session.refresh(row)
    await db_session.refresh(bot)
    assert row.status == "consumed"
    creds = decrypt_json(app.state.cipher, bot.credentials_enc, CREDENTIALS_AAD)
    assert creds["app_secret"] == "rotated-secret"
    result = (await _outbox(db_session, "card_update"))[-1]
    text = json.dumps(result.payload["card"], ensure_ascii=False)
    assert "权限补上了" in text and scope_prompt.EVENT_KEY not in text
    audit = (
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "bot.feishu_app_update"))
    ).one()
    assert audit.actor_id == user.id
    # nothing left to poll
    assert await scope_prompt.sweep(app.state.session_factory, app.state.cipher) == {
        "feishu_app_scopes_polled": 0
    }


async def test_a_pending_scan_waits_and_a_denial_offers_a_retry(app, db_session, monkeypatch):
    _, _, task, fake, row = await _started(db_session, app, monkeypatch)

    await scope_prompt.sweep(app.state.session_factory, app.state.cipher)
    await db_session.refresh(row)
    assert row.status == "pending" and len(await _outbox(db_session, "card_update")) == 1

    row.next_poll_at = None
    await db_session.commit()
    fake.results.append(registration.Poll("denied"))
    await scope_prompt.sweep(app.state.session_factory, app.state.cipher)
    await db_session.refresh(row)
    assert row.status == "denied"
    result = (await _outbox(db_session, "card_update"))[-1]
    text = json.dumps(result.payload["card"], ensure_ascii=False)
    assert "取消了确认" in text and f"personal:{task.id}" in text and "重新补齐" in text


async def test_an_admin_removed_meanwhile_is_not_applied(app, db_session, monkeypatch):
    bot, user, _, fake, row = await _started(db_session, app, monkeypatch)
    member = await db_session.get(BotMember, (bot.id, user.id))
    await db_session.delete(member)
    await db_session.commit()
    fake.results.append(registration.Poll("succeeded", "cli_test", "rotated-secret", "ou_x"))

    await scope_prompt.sweep(app.state.session_factory, app.state.cipher)
    await db_session.refresh(row)
    await db_session.refresh(bot)
    assert row.status == "cancelled"
    creds = decrypt_json(app.state.cipher, bot.credentials_enc, CREDENTIALS_AAD)
    assert creds["app_secret"] == "app-secret"
    result = (await _outbox(db_session, "card_update"))[-1]
    assert "不是这个 AI 员工的管理员" in json.dumps(result.payload["card"], ensure_ascii=False)
