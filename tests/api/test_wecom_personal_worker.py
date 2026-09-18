"""连接企业微信：本人私聊发指令、点档位卡片，企业微信确认授权人就是本人才连上。"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
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
    WecomPersonalGrant,
)
from coreman.core.prompting import Speaker
from coreman.core.wecom_personal import gateway, policy
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat import wecom_personal
from coreman.runtime.worker.chat.models import Intake
from tests.api.test_wecom_personal import (
    SENDER,
    WECOM_BOT,
    grant,
    mock_whoami,
    setup,
    supported,
)
from tests.integration.worker_helpers import build_ctx


def test_only_whole_sentence_commands_are_reserved():
    for text in (" 连接企业微信 ", "连接我的企业微信", "连接企微"):
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


async def connect(db_session, app, db_engine, *, authorizer=SENDER, token_status=200, **kw):
    bot, user, task = await setup(db_session, app, **kw)
    await supported(db_session, bot)
    intake = await intake_for(db_session, bot, task, "连接企业微信")
    ctx = build_ctx(db_engine, task)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(gateway.AUTH_URL).mock(
            return_value=httpx.Response(token_status, json={"errcode": 0, "token": "tok-pre"})
        )
        mock_whoami(mock, authorizer=authorizer)
        assert await wecom_personal.intercept(db_session, ctx, intake)
    await db_session.commit()
    return bot, user, task


async def test_connect_sends_the_tier_card_to_the_private_chat(db_session, app, db_engine):
    bot, user, task = await connect(db_session, app, db_engine)
    await db_session.refresh(task)
    assert task.status == "succeeded" and task.result == {"wecom_personal_flow": True}
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    assert "可使用权限" in stream.final_text and bot.name in stream.final_text
    card = await db_session.scalar(select(OutboxItem))
    assert card.target == {"chat_id": SENDER} and card.kind == "send"
    body = card.payload["card"]
    assert body["card_type"] == "vote_interaction"
    assert body["task_id"].startswith(f"wecom_personal@{task.id}@")
    options = body["checkbox"]["option_list"]
    assert [o["id"] for o in options] == ["readonly", "all_except_send", "all"]
    assert all(len(o["text"]) <= 11 for o in options)
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id))
    assert row.status == "selecting" and row.selection_task_id == task.id


@pytest.mark.parametrize("authorizer", ["wo-other-member-00000000000000000000", None])
async def test_connect_tells_a_non_authorizer_right_away(db_session, app, db_engine, authorizer):
    bot, user, task = await connect(db_session, app, db_engine, authorizer=authorizer)
    await db_session.refresh(task)
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    expected = "不是这个机器人" if authorizer else "还没有人授权"
    assert expected in stream.final_text and task.result["personal_connect_denied"]
    assert await db_session.scalar(select(OutboxItem)) is None
    assert await db_session.get(WecomPersonalGrant, (bot.id, user.id)) is None


async def test_connect_still_offers_the_card_when_wecom_is_unreachable(db_session, app, db_engine):
    bot, user, _ = await connect(db_session, app, db_engine, token_status=502)
    assert (await db_session.scalar(select(OutboxItem))).payload["card"]
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id))
    assert row.status == "selecting"


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
    assert await db_session.get(WecomPersonalGrant, (bot.id, user.id)) is None
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


def mock_token(mock):
    return mock.post(gateway.AUTH_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "token": "tok-new"})
    )


async def test_the_authorizer_connects_with_the_chosen_tier(db_session, app, db_engine):
    bot, user, _ = await connect(db_session, app, db_engine)
    task_id = await card_id(db_session)
    with respx.mock as mock:
        mock_token(mock)
        mock_whoami(mock)
        result, update = await click(db_session, db_engine, bot, task_id)
    assert result == "wecom_personal_connected"
    assert (
        update.target["req_id"] == "click-req"
        and "已连接" in update.payload["card"]["main_title"]["title"]
    )
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    assert row.status == "connected" and row.authorization_level == "all_except_send"
    assert row.authorizer_id == SENDER and row.token_enc and row.verified_at


async def test_an_authorizer_known_by_another_id_still_matches(db_session, app, db_engine):
    # 回调里是明文 userid，whoami 给的是密文 userid：通过身份表落到同一位员工。
    bot, user, _ = await connect(db_session, app, db_engine, sender="owner")
    task_id = await card_id(db_session)
    with respx.mock as mock:
        mock_token(mock)
        mock_whoami(mock, authorizer=SENDER)
        result, _ = await click(db_session, db_engine, bot, task_id, clicker="owner")
    assert result == "wecom_personal_connected"


@pytest.mark.parametrize("authorizer", ["wo-other-member-00000000000000000000", None])
async def test_an_authorizer_changed_before_the_click_does_not_connect(
    db_session, app, db_engine, authorizer
):
    bot, user, _ = await connect(db_session, app, db_engine)
    task_id = await card_id(db_session)
    with respx.mock as mock:
        mock_token(mock)
        mock_whoami(mock, authorizer=authorizer)
        result, update = await click(db_session, db_engine, bot, task_id)
    assert result == (
        "wecom_personal_not_authorizer" if authorizer else "wecom_personal_not_authorized"
    )
    assert "没有连接" in update.payload["card"]["main_title"]["title"]
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    assert row.status == "revoked" and row.token_enc is None


async def test_another_member_clicking_the_card_is_ignored(db_session, app, db_engine):
    bot, user, _ = await connect(db_session, app, db_engine)
    task_id = await card_id(db_session)
    with respx.mock as mock:
        result, update = await click(db_session, db_engine, bot, task_id, clicker="stranger")
        assert not mock.calls
    assert result == "ignored" and update is None
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    assert row.status == "selecting"


async def test_an_expired_card_does_not_connect(db_session, app, db_engine):
    bot, user, _ = await connect(db_session, app, db_engine)
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id))
    row.selection_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    with respx.mock as mock:
        result, update = await click(db_session, db_engine, bot, await card_id(db_session))
        assert not mock.calls
    assert result == "expired" and "失效" in update.payload["card"]["main_title"]["title"]


async def test_upstream_failure_keeps_the_card_usable(db_session, app, db_engine):
    bot, user, _ = await connect(db_session, app, db_engine)
    with respx.mock as mock:
        mock.post(gateway.AUTH_URL).mock(return_value=httpx.Response(502))
        result, _ = await click(db_session, db_engine, bot, await card_id(db_session))
    assert result == "wecom_personal_upstream_unavailable"
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    assert row.status == "selecting"


async def test_disconnect_command_revokes_locally(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "断开企业微信")
    assert await wecom_personal.intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.commit()
    row = await db_session.get(WecomPersonalGrant, (bot.id, user.id), populate_existing=True)
    assert row.status == "revoked" and row.token_enc is None


async def test_connected_private_chat_adds_tools_on_top_of_the_assistant(
    db_session, app, db_engine
):
    bot, user, task = await setup(db_session, app)
    await supported(db_session, bot)
    row = await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "看看我的待办")
    ctx = build_ctx(db_engine, task)
    base = uuid.uuid4()
    initial = {policy.PREFIX + "TOKEN": "forged", "BOT_TOKEN_ERP": "business"}
    prompt, env = await wecom_personal.configure(db_session, ctx, intake, base, "你是助理", initial)
    assert prompt.startswith("你是助理") and "## 本人企业微信" in prompt and "仅读取" in prompt
    assert env["BOT_TOKEN_ERP"] == "business"
    assert env[policy.PREFIX + "URL"].endswith("/api/runtime/wecom-personal/mcp")
    capability = policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"])
    assert (capability.task_id, capability.actor, capability.session_id) == (
        task.id,
        str(user.id),
        base,
    )
    assert capability.epoch == row.context_epoch


@pytest.mark.parametrize("denial", ["not_connected", "runtime", "group", "codex_only"])
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
        await grant(db_session, app, bot, user)
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
    await grant(db_session, app, bot, user)
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
        await grant(db_session, app, bot, user)
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
