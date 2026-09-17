from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import User, UserIdentity
from coreman.core.db.session import make_session_factory


async def test_resolve_by_platform_user_id_open_id_and_unknown(db_session: AsyncSession) -> None:
    u = User(login_name="zhangsan", display_name="张三")
    db_session.add(u)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=u.id, platform="wecom", platform_user_id="zs", open_id="woXYZ")
    )
    await db_session.commit()
    s = await resolve_speaker(db_session, platform="wecom", platform_user_id="zs")
    assert s.known and s.user_id == u.id and s.login_name == "zhangsan" and s.display_name == "张三"
    s2 = await resolve_speaker(db_session, platform="wecom", platform_user_id="woXYZ")
    assert s2.known and s2.user_id == u.id and s2.platform_user_id == "woXYZ"
    s3 = await resolve_speaker(db_session, platform="wecom", platform_user_id="nobody")
    assert not s3.known and s3.login_name is None
    s4 = await resolve_speaker(db_session, platform="wecom", platform_user_id="wo" + "A" * 40)
    assert not s4.known


async def test_disabled_identity_is_not_an_authorized_speaker(db_session: AsyncSession) -> None:
    user = User(login_name="disabled", display_name="Disabled", status="disabled")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="disabled"))
    await db_session.commit()
    speaker = await resolve_speaker(db_session, platform="wecom", platform_user_id="disabled")
    assert not speaker.known and speaker.login_name is None


async def test_identity_refresh_observes_disable_in_another_transaction(db_session, db_engine):
    user = User(login_name="recently-disabled", display_name="User")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="wecom", platform_user_id="pid"))
    await db_session.commit()
    assert (await resolve_speaker(db_session, platform="wecom", platform_user_id="pid")).known
    async with make_session_factory(db_engine)() as other:
        await other.execute(update(User).where(User.id == user.id).values(status="disabled"))
        await other.commit()
    assert not (await resolve_speaker(db_session, platform="wecom", platform_user_id="pid")).known


async def test_feishu_open_id_is_not_a_cross_app_identity(db_session):
    user = User(login_name="feishu", display_name="User")
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=user.id, platform="feishu", platform_user_id="stable", open_id="ou_app"
        )
    )
    await db_session.commit()
    assert not (
        await resolve_speaker(db_session, platform="feishu", platform_user_id="ou_app")
    ).known


async def test_feishu_union_event_resolution_and_fail_closed(db_session):
    import copy

    from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
    from coreman.core.chat import identity
    from coreman.core.db.models import InboundEvent
    from tests.integration.worker_helpers import seed_bot

    bot, _, cipher = await seed_bot(db_session)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_a", "app_secret": "secret"}, CREDENTIALS_AAD
    )
    user = User(login_name="human", display_name="Human")
    db_session.add(user)
    await db_session.flush()
    ident = UserIdentity(
        user_id=user.id, platform="feishu", platform_user_id="canonical", union_id="on_stable"
    )
    db_session.add(ident)
    payload = {
        "sender": {
            "platform_user_id": "",
            "open_id": "ou_app",
            "union_id": "on_stable",
            "sender_type": "user",
        },
        "raw": {
            "header": {"app_id": "cli_a", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_app", "union_id": "on_stable"},
                },
                "message": {"chat_id": "oc1", "chat_type": "p2p", "message_id": "om1"},
            },
        },
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id="om1",
        kind="message",
        chat_type="single",
        chat_id="oc1",
        sender_platform_user_id="",
        sender_open_id="ou_app",
        payload=payload,
        reply_context={},
    )
    db_session.add(event)
    await db_session.commit()
    resolve = getattr(identity, "resolve_feishu_event_speaker", None)
    assert resolve is not None, "durable event union resolver is missing"

    async def result():
        return await resolve(db_session, bot=bot, event=event, cipher=cipher)

    speaker = await result()
    assert speaker.user_id == user.id and speaker.platform_user_id == "canonical"
    for section, field, value in [
        ("header", "app_id", "other"),
        ("message", "chat_id", "other"),
        ("message", "message_id", "other"),
        ("message", "chat_type", "group"),
        ("sender_id", "open_id", "other"),
        ("sender_id", "union_id", "other"),
        ("sender_id", "user_id", "other"),
    ]:
        bad = copy.deepcopy(payload)
        part = (
            bad["raw"]["header"]
            if section == "header"
            else bad["raw"]["event"]["sender"]["sender_id"]
            if section == "sender_id"
            else bad["raw"]["event"][section]
        )
        part[field] = value
        event.payload = bad
        assert not (await result()).known, (section, field)
    event.payload = payload
    saved_bot_id = event.bot_id
    import uuid

    event.bot_id = uuid.uuid4()
    assert not (await result()).known
    event.bot_id = saved_bot_id
    saved_credentials = bot.credentials_enc
    bot.credentials_enc = "invalid"
    assert not (await result()).known
    bot.credentials_enc = saved_credentials
    for malformed in [[], {"raw": []}, {"sender": [], "raw": {"event": []}}]:
        event.payload = malformed
        assert not (await result()).known
    event.payload = payload
    for status, source in [("disabled", "sync"), ("active", "bootstrap")]:
        user.status, user.source = status, source
        await db_session.flush()
        assert not (await result()).known
    user.status, user.source = "active", "sync"
    duplicate = UserIdentity(
        user_id=user.id, platform="feishu", platform_user_id="duplicate", union_id="on_stable"
    )
    db_session.add(duplicate)
    await db_session.flush()
    assert not (await result()).known
    await db_session.delete(duplicate)
    await db_session.flush()
    explicit = copy.deepcopy(payload)
    explicit["sender"]["platform_user_id"] = "canonical"
    explicit["raw"]["event"]["sender"]["sender_id"]["user_id"] = "canonical"
    event.sender_platform_user_id = "canonical"
    event.payload = explicit
    assert (await result()).platform_user_id == "canonical"
    ident.union_id = None
    await db_session.flush()
    assert (await result()).platform_user_id == "canonical" and (await result()).known
    ident.union_id = "conflicting"
    await db_session.flush()
    assert not (await result()).known
