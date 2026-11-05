"""通过 API 驱动 AI 员工：发起校验、在飞书会话里跑一轮、开轮前复核、群别名命令与状态。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from coreman.core.api_tasks import aliases, service, tokens
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.bus import instances, tasks
from coreman.core.chat import human_collaboration
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    ApiTask,
    BotConversationAlias,
    ChatLog,
    InboundEvent,
    OutboxItem,
    TaskStream,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import MASTER, build_ctx, seed_bot, turn_block


async def _setup(session, *, api_access=True, reached=True):  # type: ignore[no-untyped-def]
    bot, relay, cipher = await seed_bot(session)
    bot.platform, bot.api_access = "feishu", api_access
    owner = User(login_name="zs", display_name="张三", email="zs@example.test")
    session.add(owner)
    await session.flush()
    session.add(UserIdentity(user_id=owner.id, platform="feishu", platform_user_id="zs"))
    if reached:
        session.add(UserReached(bot_id=bot.id, user_id=owner.id, platform_chat_id="p2p-zs"))
    minted = await tokens.mint(session, owner, name="脚本", bot_ids=[bot.id], days=30)
    await session.commit()
    return bot, owner, minted


async def _create(session, minted, owner, **kw):  # type: ignore[no-untyped-def]
    args = {
        "bot_key": "sales_bot",
        "prompt": "整理今天的告警",
        "request_id": None,
        "conversation": {},
        **kw,
    }
    row, created = await service.create(
        session, token=minted.row, user=owner, cipher=Cipher(MASTER), **args
    )
    await session.commit()
    return row, created


async def _claim(session):  # type: ignore[no-untyped-def]
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed is not None
    return claimed


async def _run(engine, task, relay=None):  # type: ignore[no-untyped-def]
    fake = relay or FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    return fake


async def test_p2p_round_trip_runs_in_private_chat_and_returns_reply(db_engine, db_session):
    bot, owner, minted = await _setup(db_session)
    row, created = await _create(db_session, minted, owner)
    assert created and row.target == "p2p" and row.chat_id == "p2p-zs"
    queued = await tasks.get(db_session, row.task_id)
    event = await db_session.get(InboundEvent, queued.inbound_event_id)
    assert event.platform_msg_id == f"api:{row.id}" and event.sender_platform_user_id == "zs"
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    assert trigger.target == {
        "chat_id": "p2p-zs",
        "recipient_user_id": str(owner.id),
        "recipient_platform_user_id": "zs",
    }
    assert "**张三** 通过 API 发起（令牌：脚本）" in trigger.payload["markdown"]
    assert (await service.view(db_session, row))["status"] == "queued"

    task = await _claim(db_session)
    assert task.payload["api_task_id"] == str(row.id) and task.session_key == "p2p-zs"
    fake = await _run(db_engine, task)
    block = turn_block(fake.requests[0])
    assert "通过 API（令牌「脚本」）从外部程序发起" in block
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    # 没有可回复的消息：直接发在私聊里。
    assert stream.reply_context["chat_id"] == "p2p-zs" and not stream.reply_context.get(
        "message_id"
    )
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert log.user_id == owner.id and log.session_key == "p2p-zs"
    view = await service.view(db_session, row)
    assert view["status"] == "succeeded" and "你好，世界" in view["reply"]


async def test_create_checks_bot_permissions_and_conversation(db_session):
    bot, owner, minted = await _setup(db_session, api_access=False, reached=False)
    with pytest.raises(TokenError) as denied:
        await _create(db_session, minted, owner)
    assert denied.value.status == 403 and "允许 API 驱动" in denied.value.message
    bot.api_access = True
    await db_session.commit()
    with pytest.raises(TokenError) as no_chat:
        await _create(db_session, minted, owner)
    assert no_chat.value.status == 409 and "私聊" in no_chat.value.message
    with pytest.raises(TokenError) as missing:
        await _create(db_session, minted, owner, conversation={"target": "chat", "alias": "订单"})
    assert missing.value.status == 404
    with pytest.raises(TokenError) as other_bot:
        await _create(db_session, minted, owner, bot_key="nobody")
    assert other_bot.value.status == 404
    assert await db_session.scalar(select(ApiTask.id)) is None


async def test_group_alias_requires_membership(db_session):
    bot, owner, minted = await _setup(db_session)
    db_session.add(BotConversationAlias(bot_id=bot.id, alias="订单异常", chat_id="oc_group"))
    await db_session.commit()
    conversation = {"target": "chat", "alias": "订单异常"}
    with patch.object(human_collaboration, "in_group", new=AsyncMock(return_value=False)):
        with pytest.raises(TokenError) as outside:
            await _create(db_session, minted, owner, conversation=conversation)
    assert outside.value.status == 403
    with patch.object(human_collaboration, "in_group", new=AsyncMock(return_value=True)) as check:
        row, _ = await _create(db_session, minted, owner, conversation=conversation)
    assert check.await_args.args[1:3] == ("oc_group", "zs")
    assert row.target == "chat" and row.chat_id == "oc_group" and row.alias == "订单异常"
    task = await tasks.get(db_session, row.task_id)
    assert task.session_key == "oc_group" and task.payload["serialize_session"] is True
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    # 群里不带收件人复核。
    assert trigger.target == {"chat_id": "oc_group"}


async def test_request_id_replays_and_open_limit(db_session):
    _, owner, minted = await _setup(db_session)
    first, created = await _create(db_session, minted, owner, request_id="r-1")
    again, created_again = await _create(db_session, minted, owner, request_id="r-1")
    assert created and not created_again and again.id == first.id
    with pytest.raises(TokenError) as conflict:
        await _create(db_session, minted, owner, request_id="r-1", prompt="别的事")
    assert conflict.value.status == 409
    for i in range(service.MAX_OPEN_PER_TOKEN - 1):
        await _create(db_session, minted, owner, request_id=f"r-{i + 2}")
    with pytest.raises(TokenError) as busy:
        await _create(db_session, minted, owner)
    assert busy.value.status == 429


async def test_revoked_before_run_is_refused_in_the_chat(db_engine, db_session):
    _, owner, minted = await _setup(db_session)
    row, _ = await _create(db_session, minted, owner)
    minted.row.revoked_at = utcnow()
    await db_session.commit()
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    view = await service.view(db_session, row)
    assert view["status"] == "failed" and view["error_code"] == "api_unavailable"
    assert "吊销" in view["error_message"]
    refused = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:refused")
    )
    assert refused.payload["markdown"].startswith("这条 API 任务没有执行")


async def test_superseded_and_cancelled_statuses(db_session):
    _, owner, minted = await _setup(db_session)
    row, _ = await _create(db_session, minted, owner)
    assert await service.cancel(db_session, row)
    await tasks.finish(db_session, row.task_id, status="cancelled", error_code="api_cancel")
    await db_session.commit()
    assert (await service.view(db_session, row))["status"] == "cancelled"
    other, _ = await _create(db_session, minted, owner)
    await tasks.request_cancel(db_session, other.task_id, "superseded")
    await tasks.finish(db_session, other.task_id, status="cancelled", error_code="superseded")
    await db_session.commit()
    assert (await service.view(db_session, other))["status"] == "superseded"
    queued, _ = await _create(db_session, minted, owner)
    await service.cancel(db_session, queued)
    await db_session.commit()
    # 还没被认领就取消：直接算取消，也不再占名额。
    assert (await service.view(db_session, queued))["status"] == "cancelled"


async def test_group_turn_rechecks_membership_and_never_replies_to_synthetic_ids(
    db_engine, db_session
):
    from coreman.core.api_tasks import reply_to

    assert reply_to("api:123") is None and reply_to("om_x") == "om_x" and reply_to(None) is None
    bot, owner, minted = await _setup(db_session)
    db_session.add(BotConversationAlias(bot_id=bot.id, alias="值班", chat_id="oc_g"))
    await db_session.commit()
    conversation = {"target": "chat", "alias": "值班"}
    with patch.object(human_collaboration, "in_group", new=AsyncMock(return_value=True)):
        row, _ = await _create(db_session, minted, owner, conversation=conversation)
    task = await _claim(db_session)
    with patch.object(human_collaboration, "in_group", new=AsyncMock(return_value=False)):
        fake = await _run(db_engine, task)
    assert fake.requests == []
    view = await service.view(db_session, row)
    assert view["status"] == "failed" and "还在这个群里" in view["error_message"]


def test_alias_command_parsing():
    assert aliases.parse("/api") == ("show", "")
    assert aliases.parse("/api 绑定 订单-异常_1") == ("bind", "订单-异常_1")
    assert aliases.parse("/API bind Orders") == ("bind", "Orders")
    assert aliases.parse("/api 解绑") == ("unbind", "")
    assert aliases.parse("/api 绑定 有 空格")[0] == "invalid"
    assert aliases.parse("api 绑定 订单") is None  # 不带斜杠是在跟 AI 员工说话
    assert aliases.parse("API") is None
    assert aliases.parse("/api 怎么用") is None
    assert aliases.parse("帮我调一下 api") is None


async def test_group_alias_commands(db_engine, db_session):
    bot, _, _ = await _setup(db_session)
    task = await chat_task(
        db_session, bot, "/api 绑定 订单异常", sender="zs", chat_type="group", chat_id="oc_g"
    )
    await _run(db_engine, task)
    row = await db_session.scalar(select(BotConversationAlias))
    assert row.alias == "订单异常" and row.chat_id == "oc_g"
    reply = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    assert "已绑定" in (reply.final_text or "")
    second = await chat_task(
        db_session, bot, "/api 解绑", sender="zs", chat_type="group", chat_id="oc_g"
    )
    await _run(db_engine, second)
    assert await db_session.scalar(select(BotConversationAlias)) is None
