"""私聊里的「连接 / 断开企业微信」：没绑定给出绑定入口，已绑定用卡片选档位，断开只是暂停。"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import respx
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import (
    CronRun,
    InboundEvent,
    OutboxItem,
    RelayServer,
    TaskStream,
    UserReached,
    WecomPersonalBinding,
)
from coreman.core.prompting import Speaker
from coreman.core.wecom_personal import policy
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat import wecom_personal
from coreman.runtime.worker.chat.models import Intake
from tests.api.test_wecom_personal import (
    SENDER,
    WECOM_BOT,
    bind,
    setup,
    supported,
)
from tests.integration.worker_helpers import build_ctx


def test_only_whole_sentence_commands_are_reserved():
    for text in (" 连接企业微信 ", "连接我的企业微信", "连接企微", "绑定企业微信"):
        assert wecom_personal.connect_requested(text)
    for text in ("/connect", "请连接企业微信", "企业微信", "连接飞书"):
        assert not wecom_personal.connect_requested(text)
    assert wecom_personal.disconnect_requested("断开企业微信")
    assert not wecom_personal.disconnect_requested("断开")


async def intake_for(session, bot, task, text):
    event = await session.get(InboundEvent, task.inbound_event_id)
    relay = await session.get(RelayServer, bot.relay_server_id)
    speaker = await resolve_speaker(
        session, platform="wecom", platform_user_id=event.sender_platform_user_id
    )
    return Intake(
        bot, relay, event, speaker, event.chat_id, event.chat_type, task.session_key, text, "text"
    )


async def connect(db_session, app, db_engine, *, bound=True, **kw):
    bot, user, task = await setup(db_session, app, **kw)
    await supported(db_session, bot)
    row = await bind(db_session, app, user, level="all_except_send") if bound else None
    intake = await intake_for(db_session, bot, task, "连接企业微信")
    ctx = build_ctx(db_engine, task)
    with respx.mock(assert_all_called=False) as mock:
        assert await wecom_personal.intercept(db_session, ctx, intake)
        # 连接只是选档位：不向企业微信发任何请求。
        assert not mock.calls
    await db_session.commit()
    return bot, user, task, row


async def final_text(db_session, task):
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    return stream.final_text


async def test_connect_without_a_binding_offers_a_one_tap_link(db_session, app, db_engine):
    bot, user, task, _ = await connect(db_session, app, db_engine, bound=False)
    await db_session.refresh(task)
    assert task.result == {"wecom_personal_flow": True, "wecom_personal_unbound": True}
    text = await final_text(db_session, task)
    assert "(http://localhost/my-wecom?authorize=1)" in text and "确认授权" in text
    # 私聊里不出现企业微信的授权地址：它能换回授权机器人的 Secret，只在本人登录的页面上给出。
    assert "work.weixin.qq.com" not in text
    assert await db_session.scalar(select(OutboxItem)) is None
    row = await db_session.get(WecomPersonalBinding, user.id)
    assert row.status == "unbound" and row.scan_status is None
    assert row.scan_notify["chat_id"] == SENDER and row.scan_notify["task_id"] == task.id
    assert row.scan_notify["bot_id"] == str(bot.id)


async def test_binding_from_chat_announces_the_result_and_the_card_connects(
    db_session, app, db_engine, monkeypatch
):
    from coreman.core.wecom_personal import binding
    from tests.api.test_wecom_binding import mock_wecom
    from tests.api.test_wecom_bot_provisions import _bound, _fake
    from tests.api.test_wecom_personal import PERSONAL_BOT, PERSONAL_SECRET

    fake = _fake(monkeypatch)
    bot, user, _, _ = await connect(db_session, app, db_engine, bound=False)
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    await binding.start(app.state.cipher, row)
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row.scan_next_poll_at = None
    with respx.mock as mock:
        mock_wecom(mock, probe={"mail": 850002})
        await binding.refresh(db_session, app.state.cipher, row)
    await db_session.commit()
    assert row.status == "bound" and row.scan_notify is None
    sent = (await db_session.scalars(select(OutboxItem).order_by(OutboxItem.id))).all()
    assert [item.target for item in sent] == [{"chat_id": SENDER}] * 2
    assert "已绑定企业微信" in sent[0].payload["markdown"]
    assert "邮件" in sent[0].payload["markdown"]
    card_task = sent[1].payload["card"]["task_id"]
    result, update = await click(db_session, db_engine, bot, card_task, level="all")
    assert result == "wecom_personal_connected"
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.authorization_level == "all"


@pytest.mark.parametrize("stale", [False, True])
async def test_a_rejected_binding_is_explained_in_the_chat(
    db_session, app, db_engine, monkeypatch, stale
):
    from coreman.core.wecom_personal import binding
    from tests.api.test_wecom_binding import mock_wecom
    from tests.api.test_wecom_bot_provisions import _bound, _fake
    from tests.api.test_wecom_personal import PERSONAL_BOT, PERSONAL_SECRET

    fake = _fake(monkeypatch)
    _, user, _, _ = await connect(db_session, app, db_engine, bound=False)
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    if stale:
        row.scan_notify = {
            **row.scan_notify,
            "at": (datetime.now(UTC) - binding.NOTIFY_TTL - timedelta(minutes=1)).isoformat(),
        }
    await binding.start(app.state.cipher, row)
    fake.results.append(_bound(PERSONAL_BOT, PERSONAL_SECRET))
    row.scan_next_poll_at = None
    with respx.mock as mock:
        mock_wecom(mock, authorizer="wo-stranger-0000000000000000000000")
        await binding.refresh(db_session, app.state.cipher, row)
    await db_session.commit()
    sent = (await db_session.scalars(select(OutboxItem))).all()
    if stale:
        assert sent == []
    else:
        assert len(sent) == 1 and "没有绑定" in sent[0].payload["markdown"]
    assert row.scan_notify is None


async def test_connect_after_the_bot_was_deleted_asks_to_rebind(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    await supported(db_session, bot)
    db_session.add(
        WecomPersonalBinding(user_id=user.id, status="unbound", error="credentials_rejected")
    )
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "连接企业微信")
    assert await wecom_personal.intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.commit()
    assert "已被删除或重置了 Secret" in await final_text(db_session, task)


async def test_connect_sends_the_tier_card_to_the_private_chat(db_session, app, db_engine):
    bot, user, task, row = await connect(db_session, app, db_engine)
    await db_session.refresh(task)
    assert task.status == "succeeded" and task.result == {"wecom_personal_flow": True}
    text = await final_text(db_session, task)
    assert row.bot_name in text and "http://localhost/my-wecom" in text
    card = await db_session.scalar(select(OutboxItem))
    assert card.target == {"chat_id": SENDER} and card.kind == "send"
    body = card.payload["card"]
    assert body["card_type"] == "vote_interaction"
    assert body["task_id"].startswith(f"wecom_personal@{task.id}@")
    options = body["checkbox"]["option_list"]
    assert [o["id"] for o in options] == ["readonly", "all_except_send", "all"]
    # 预先勾上本人当前的档位。
    assert [o["id"] for o in options if o["is_checked"]] == ["all_except_send"]
    assert all(len(o["text"]) <= 11 for o in options)


async def test_the_brief_lists_capabilities_that_need_renewal(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    await supported(db_session, bot)
    await bind(
        db_session,
        app,
        user,
        capabilities={"mail:read": {"state": "expired"}, "todo:read": {"state": "unauthorized"}},
    )
    intake = await intake_for(db_session, bot, task, "连接企业微信")
    assert await wecom_personal.intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.commit()
    text = await final_text(db_session, task)
    assert "已过期：邮件" in text and "未授权：待办" in text and "电脑端企业微信" in text


@pytest.mark.parametrize("denial", ["runtime", "unbound", "group"])
async def test_connect_explains_why_it_cannot_start(db_session, app, db_engine, denial):
    bot, user, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    if denial != "runtime":
        await supported(db_session, bot)
    intake = await intake_for(db_session, bot, task, "连接企业微信")
    if denial == "unbound":
        intake = replace(intake, speaker=Speaker(SENDER, None, None, None))
    ctx = build_ctx(db_engine, task)
    handled = await wecom_personal.intercept(db_session, ctx, intake)
    await db_session.commit()
    # 群聊里这句话只是普通消息，交给助手。
    assert handled is (denial != "group")
    assert await db_session.get(WecomPersonalBinding, user.id) is None
    assert await db_session.scalar(select(OutboxItem)) is None


async def click(
    db_session, db_engine, bot, card_task_id, *, level="all_except_send", clicker=SENDER
):
    body = {
        "msgid": f"e-{uuid.uuid4()}",
        "aibotid": WECOM_BOT,
        "chattype": "single",
        "from": {"userid": clicker},
        "msgtype": "event",
        "event": {"eventtype": "template_card_event"},
    }
    action = {
        "task_id": card_task_id,
        "card_type": "vote_interaction",
        "event_key": "wecom_personal_connect",
        "selected": {wecom_personal.QUESTION_KEY: [level]},
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=body["msgid"],
        kind="card_action",
        chat_type="single",
        chat_id=clicker,
        sender_platform_user_id=clicker,
        payload={"card_action": action, "raw": {"cmd": "aibot_event_callback", "body": body}},
        reply_context={"req_id": "click-req"},
    )
    db_session.add(event)
    await db_session.flush()
    await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": clicker, "chat_id": clicker},
            session_key=clicker,
            inbound_event_id=event.id,
            dedupe_key=f"inbound:{event.id}",
        ),
    )
    await db_session.commit()
    task = await tasks.claim(db_session, lane="fast", instance_id="worker-test")
    await db_session.commit()
    await CardActionHandler().run(build_ctx(db_engine, task))
    await db_session.refresh(task)
    update = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update", OutboxItem.bot_id == bot.id)
    )
    return task.result["card"], update


async def card_id(db_session):
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    return card.payload["card"]["task_id"]


async def test_choosing_a_tier_enables_the_binding(db_session, app, db_engine):
    bot, user, _, row = await connect(db_session, app, db_engine)
    epoch = row.context_epoch
    row.enabled = False
    await db_session.commit()
    with respx.mock as mock:
        result, update = await click(
            db_session, db_engine, bot, await card_id(db_session), level="all"
        )
        assert not mock.calls
    assert result == "wecom_personal_connected"
    assert update.target["req_id"] == "click-req"
    assert "已连接" in update.payload["card"]["main_title"]["title"]
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.authorization_level == "all" and row.enabled and row.context_epoch != epoch


async def test_another_member_clicking_the_card_is_ignored(db_session, app, db_engine):
    bot, user, _, _ = await connect(db_session, app, db_engine)
    result, update = await click(
        db_session, db_engine, bot, await card_id(db_session), clicker="stranger"
    )
    assert result == "ignored" and update is None
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.authorization_level == "all_except_send"


async def test_an_expired_card_does_not_change_anything(db_session, app, db_engine):
    bot, user, task, _ = await connect(db_session, app, db_engine)
    await db_session.refresh(task)
    task.finished_at = datetime.now(UTC) - wecom_personal.SELECTION_TTL - timedelta(seconds=1)
    await db_session.commit()
    result, update = await click(db_session, db_engine, bot, await card_id(db_session))
    assert result == "expired" and "失效" in update.payload["card"]["main_title"]["title"]
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.authorization_level == "all_except_send"


async def test_a_binding_removed_before_the_click_is_reported(db_session, app, db_engine):
    bot, user, _, row = await connect(db_session, app, db_engine)
    row.status = "unbound"
    await db_session.commit()
    result, update = await click(db_session, db_engine, bot, await card_id(db_session))
    assert result == "wecom_personal_unbound"
    assert "没有连接" in update.payload["card"]["main_title"]["title"]


async def test_disconnect_only_pauses_and_keeps_the_credentials(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user)
    epoch = row.context_epoch
    intake = await intake_for(db_session, bot, task, "断开企业微信")
    assert await wecom_personal.intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.commit()
    row = await db_session.get(WecomPersonalBinding, user.id, populate_existing=True)
    assert row.status == "bound" and not row.enabled and row.credentials_enc
    assert row.context_epoch != epoch
    assert "不需要重新扫码" in await final_text(db_session, task)


async def test_disconnect_without_a_binding_says_so(db_session, app, db_engine):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "断开企业微信")
    assert await wecom_personal.intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.commit()
    assert "还没有绑定" in await final_text(db_session, task)


async def test_connected_private_chat_adds_tools_on_top_of_the_assistant(
    db_session, app, db_engine
):
    bot, user, task = await setup(db_session, app)
    await supported(db_session, bot)
    row = await bind(db_session, app, user, capabilities={"mail:read": {"state": "expired"}})
    intake = await intake_for(db_session, bot, task, "看看我的待办")
    ctx = build_ctx(db_engine, task)
    base = uuid.uuid4()
    initial = {policy.PREFIX + "TOKEN": "forged", "BOT_TOKEN_ERP": "business"}
    prompt, env = await wecom_personal.configure(db_session, ctx, intake, base, "你是助理", initial)
    assert prompt.startswith("你是助理") and "## 本人企业微信" in prompt and "仅读取" in prompt
    assert "已过期：邮件" in prompt
    assert env["BOT_TOKEN_ERP"] == "business"
    assert env[policy.PREFIX + "URL"].endswith("/api/runtime/wecom-personal/mcp")
    capability = policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"])
    assert (capability.task_id, capability.actor, capability.session_id) == (
        task.id,
        str(user.id),
        base,
    )
    assert capability.epoch == row.context_epoch


@pytest.mark.parametrize("denial", ["not_connected", "paused", "runtime", "group", "codex_only"])
async def test_no_tools_without_a_live_connection(db_session, app, db_engine, denial):
    bot, user, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    if denial != "runtime":
        capabilities = (
            {"codex": {policy.RUNTIME_CAPABILITY: True}} if denial == "codex_only" else None
        )
        await supported(db_session, bot, capabilities)
    if denial != "not_connected":
        await bind(db_session, app, user, enabled=denial != "paused")
    intake = await intake_for(db_session, bot, task, "看看我的待办")
    result = await wecom_personal.configure(
        db_session,
        build_ctx(db_engine, task),
        intake,
        uuid.uuid4(),
        "p",
        {policy.PREFIX + "URL": "x"},
    )
    assert result == ("p", {})


@pytest.mark.parametrize("variation", ["owner", "group_target"])
async def test_owner_only_schedule_can_use_the_tools(app, db_session, db_engine, variation):
    from coreman.core.db.models import BotMember, CronJob
    from tests.api.test_personal_schedules import run_due

    bot, user, _ = await setup(db_session, app)
    await supported(db_session, bot)
    await bind(db_session, app, user)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id=SENDER))
    job = CronJob(
        bot_id=bot.id,
        created_by=user.id,
        name="待办汇总",
        cron_expression="* * * * *",
        prompt="汇总我今天到期的待办",
        next_run_at=datetime.now(UTC),
        target_chats=["group-1"] if variation == "group_target" else [],
    )
    db_session.add(job)
    await db_session.commit()
    task, fake = await run_due(db_session, db_engine, job)
    request = fake.requests[0]
    env = request["env_vars"]
    run = await db_session.scalar(select(CronRun))
    if variation == "group_target":
        # 结果要发到群里：本人的企业微信不能用。
        assert policy.PREFIX + "TOKEN" not in env and not run.private
        return
    capability = policy.read_capability(app.state.cipher, env[policy.PREFIX + "TOKEN"])
    assert capability.session_id is None and capability.task_id == task.id
    assert "## 本人企业微信" in request["messages"][0]["content"]
    assert run.private


@pytest.mark.parametrize("connected", [True, False])
async def test_a_connected_turn_mounts_the_tools_and_is_owner_only(
    app, client, db_session, db_engine, connected
):
    from coreman.core.db.models import BotMember, ChatLog, UserIdentity
    from tests.api.conftest import login_as, login_existing
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    bot, user, task = await setup(db_session, app)
    await supported(db_session, bot)
    if connected:
        await bind(db_session, app, user)
    fake = FakeRelay("normal")
    await run(db_engine, task, fake)
    env = fake.requests[0]["env_vars"]
    assert (policy.PREFIX + "TOKEN" in env) is connected
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert log.private is connected
    admin = await login_as(client, db_session, role="platform_admin")
    db_session.add(BotMember(bot_id=bot.id, user_id=admin.id))
    db_session.add(UserIdentity(user_id=admin.id, platform="wecom", platform_user_id="admin"))
    await db_session.commit()
    listed = (await client.get("/api/admin/chat-logs")).json()["data"]["items"]
    # 没连接的企微私聊照旧对管理员可见；挂了本人企业微信工具的只给本人看。
    assert [item["id"] for item in listed] == ([] if connected else [log.id])
    await login_existing(client, db_session, user)
    listed = (await client.get("/api/admin/chat-logs")).json()["data"]["items"]
    assert [item["id"] for item in listed] == [log.id]
