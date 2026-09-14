from datetime import UTC, datetime

from sqlalchemy import select

from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.db.models import Escalation, OutboxItem, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.core.escalations import service
from coreman.core.platforms.feishu import FeishuClient
from coreman.runtime.scheduler.notifications import deliver_one
from tests.api.test_escalations import setup


async def test_feishu_escalation_sends_with_stable_identity_and_requires_current_quote(
    client, db_session, db_engine, monkeypatch
):
    bot, recipient, app, cipher = await setup(client, db_session)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_test", "app_secret": "synthetic"}, CREDENTIALS_AAD
    )
    app.platform, app.app_id, app.corp_id = "feishu", "cli_test", None
    db_session.add(
        UserIdentity(user_id=recipient.id, platform="feishu", platform_user_id="feishu_recipient")
    )
    await db_session.commit()
    result = await client.post(
        "/api/infra/escalations",
        json={
            "bot_key": bot.bot_key,
            "platform": "feishu",
            "target_user_ids": [str(recipient.id)],
            "question": "请确认",
            "platform_app_id": str(app.id),
        },
    )
    assert result.status_code == 200, result.text
    identity = result.json()["data"]["escalation_id"]
    calls = []

    async def notify(self, user_id, content, **kwargs):
        calls.append((user_id, content, kwargs))
        return "om_notification"

    monkeypatch.setattr(FeishuClient, "notify_user", notify)
    factory = make_session_factory(db_engine)
    assert await deliver_one(factory, cipher)
    assert calls[0][0] == "feishu_recipient" and "引用" in calls[0][1]
    async with factory() as session:
        row = await session.scalar(select(Escalation).where(Escalation.escalation_id == identity))
        assert row.notify_platform == "feishu_bot" and row.notify_message_id == "om_notification"
        now = datetime.now(UTC)
        assert (
            await service.add_reply(
                session,
                user=recipient,
                app=app,
                content="wrong",
                msg_id="m1",
                created_at=now,
                now=now,
                parent_message_id="other_question",
            )
            is None
        )
        row = await service.add_reply(
            session,
            user=recipient,
            app=app,
            content="confirmed",
            msg_id="m2",
            created_at=now,
            now=now,
            parent_message_id="om_notification",
        )
        assert row.status == "replied" and row.replies[-1]["content"] == "confirmed"
        await session.commit()
    assert not await deliver_one(factory, cipher)


async def test_feishu_notification_permanent_failure_does_not_retry_forever(
    client, db_session, db_engine, monkeypatch
):
    from coreman.core.platforms.feishu import FeishuError

    bot, recipient, app, cipher = await setup(client, db_session)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_test", "app_secret": "synthetic"}, CREDENTIALS_AAD
    )
    app.platform, app.app_id, app.corp_id = "feishu", "cli_test", None
    db_session.add(
        UserIdentity(user_id=recipient.id, platform="feishu", platform_user_id="recipient")
    )
    await db_session.commit()
    result = await client.post(
        "/api/infra/escalations",
        json={
            "bot_key": bot.bot_key,
            "platform": "feishu",
            "target_user_ids": [str(recipient.id)],
            "question": "确认",
        },
    )
    assert result.status_code == 200

    async def notify(*args, **kwargs):
        raise FeishuError(230013)

    monkeypatch.setattr(FeishuClient, "notify_user", notify)
    assert await deliver_one(make_session_factory(db_engine), cipher)
    item = await db_session.scalar(select(OutboxItem))
    assert item.status == "failed" and "230013" in item.last_error


async def test_feishu_quoted_reply_is_consumed_without_chat_history(
    client, db_session, db_engine, monkeypatch
):
    from coreman.core.db.models import ChatLog, ChatSession, InboundEvent, UserReached
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.integration.test_chat_handler import chat_task
    from tests.integration.worker_helpers import build_ctx

    bot, recipient, app, cipher = await setup(client, db_session)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_test", "app_secret": "synthetic"}, CREDENTIALS_AAD
    )
    app.platform, app.app_id = "feishu", "cli_test"
    db_session.add(
        UserIdentity(user_id=recipient.id, platform="feishu", platform_user_id="recipient")
    )
    await db_session.commit()
    created = await client.post(
        "/api/infra/escalations",
        json={
            "bot_key": bot.bot_key,
            "platform": "feishu",
            "target_user_ids": [str(recipient.id)],
            "question": "确认",
        },
    )
    assert created.status_code == 200

    async def notify(*args, **kwargs):
        return "om_notification"

    monkeypatch.setattr(FeishuClient, "notify_user", notify)
    assert await deliver_one(make_session_factory(db_engine), cipher)
    task = await chat_task(db_session, bot, "确认好了", sender="recipient")
    inbound = await db_session.get(InboundEvent, task.inbound_event_id)
    inbound.platform = "feishu"
    inbound.reply_context = {
        "parent_id": "om_notification",
        "create_time": str(int(datetime.now(UTC).timestamp() * 1000)),
    }
    await db_session.commit()
    ctx = build_ctx(db_engine, task)
    ctx.cipher = cipher
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    async with make_session_factory(db_engine)() as session:
        row = await session.scalar(select(Escalation))
        assert row.replies[0]["content"] == "确认好了"
        assert await session.scalar(select(ChatSession)) is None
        assert await session.scalar(select(ChatLog)) is None
        assert await session.scalar(select(UserReached)) is None
