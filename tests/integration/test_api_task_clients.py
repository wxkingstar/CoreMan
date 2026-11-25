"""业务系统代员工发起 API 任务：按邮箱前缀认人、独立会话、私聊推送与尾注、开轮前复核。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from coreman.core.api_tasks import service
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.bus import tasks
from coreman.core.db.models import (
    ApiClient,
    ChatLog,
    ChatSession,
    OutboxItem,
    TaskStream,
    User,
    UserIdentity,
    UserReached,
)
from tests.integration.test_api_tasks import _claim, _run, _trigger_sent
from tests.integration.worker_helpers import seed_bot, turn_block


async def _setup(session, *, reached=True):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform, bot.api_access = "feishu", True
    member = User(login_name="u-alice", display_name="爱丽丝", email="Alice@example.test")
    session.add(member)
    await session.flush()
    session.add(UserIdentity(user_id=member.id, platform="feishu", platform_user_id="ou_alice"))
    if reached:
        session.add(UserReached(bot_id=bot.id, user_id=member.id, platform_chat_id="p2p-alice"))
    client = ApiClient(
        app_key="intake",
        name="资料录入",
        scopes=["tasks"],
        bot_ids=[bot.id],
        secret_enc=cipher.encrypt("client-secret", "api_clients.secret_enc"),
    )
    session.add(client)
    await session.commit()
    return bot, member, client


async def _create(session, client, **kw):  # type: ignore[no-untyped-def]
    args = {
        "subject": "alice",
        "bot_key": "sales_bot",
        "prompt": "识别截图里的账号信息",
        "request_id": None,
        **kw,
    }
    row, created = await service.create_for_client(session, client=client, **args)
    await session.commit()
    return row, created


async def test_runs_in_its_own_session_and_replies_in_private_chat(db_engine, db_session):
    _, member, client = await _setup(db_session)
    row, created = await _create(db_session, client)
    assert created and row.client_key == "intake" and row.token_id is None
    assert row.user_id == member.id and row.chat_id == "p2p-alice"
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    assert trigger.target["chat_id"] == "p2p-alice"
    assert trigger.target["recipient_user_id"] == str(member.id)
    assert "**爱丽丝** 在「资料录入」里发起" in trigger.payload["markdown"]
    view = await service.view(db_session, row)
    assert view["user"] == "alice" and "conversation" not in view and view["status"] == "queued"

    await _trigger_sent(db_session, row)
    task = await _claim(db_session)
    # 独立会话：会话键不是私聊的 chat_id，私聊里的消息替代不了它。
    assert task.session_key == f"api:{row.id}" and task.payload["api_client"] == "intake"
    fake = await _run(db_engine, task)
    block = turn_block(fake.requests[0])
    assert "业务系统「资料录入」里发起" in block and "独立会话" in block
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    assert stream.reply_context["chat_id"] == "p2p-alice"
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert log.user_id == member.id and log.session_key == f"api:{row.id}"
    assert "不在当前对话上下文中" in log.response_content
    sessions = set(await db_session.scalars(select(ChatSession.session_key)))
    assert "p2p-alice" not in sessions
    view = await service.view(db_session, row)
    assert view["status"] == "succeeded" and "你好，世界" in view["reply"]
    # 尾注与完成标记只给人看，不返回给业务系统。
    assert "不在当前对话上下文中" not in view["reply"]
    assert not view["reply"].endswith("✅ 回复已生成")


async def test_member_is_resolved_by_unique_email_prefix(db_session):
    _, _, client = await _setup(db_session)
    with pytest.raises(TokenError) as unknown:
        await _create(db_session, client, subject="bob")
    assert unknown.value.status == 403
    twin = User(login_name="u-alice-2", display_name="另一位", email="alice@other.test")
    db_session.add(twin)
    await db_session.commit()
    with pytest.raises(TokenError) as ambiguous:
        await _create(db_session, client, subject="alice")
    assert ambiguous.value.status == 403
    # 停用账号撞前缀同样认不出：签业务令牌时也会拒签。
    twin.status = "disabled"
    await db_session.commit()
    with pytest.raises(TokenError) as disabled_twin:
        await _create(db_session, client, subject="alice")
    assert disabled_twin.value.status == 403


async def test_create_checks_client_bots_switch_and_private_chat(db_session):
    bot, _, client = await _setup(db_session, reached=False)
    with pytest.raises(TokenError) as no_chat:
        await _create(db_session, client)
    assert no_chat.value.status == 409 and "私聊" in no_chat.value.message
    bot.api_access = False
    await db_session.commit()
    with pytest.raises(TokenError) as off:
        await _create(db_session, client)
    assert off.value.status == 403 and "允许 API 驱动" in off.value.message
    client.bot_ids = []
    await db_session.commit()
    with pytest.raises(TokenError) as not_granted:
        await _create(db_session, client)
    assert not_granted.value.status == 403 and "调用方" in not_granted.value.message


async def test_request_id_replays_per_client_and_rejects_changed_content(db_session):
    _, _, client = await _setup(db_session)
    first, created = await _create(db_session, client, request_id="form-1")
    again, replayed = await _create(db_session, client, request_id="form-1")
    assert created and not replayed and again.id == first.id
    with pytest.raises(TokenError) as changed:
        await _create(db_session, client, request_id="form-1", prompt="别的事")
    assert changed.value.status == 409


async def test_open_limit_is_per_member(db_session, monkeypatch):
    _, _, client = await _setup(db_session)
    monkeypatch.setattr(service, "MAX_OPEN_PER_CLIENT_USER", 2)
    await _create(db_session, client)
    await _create(db_session, client)
    with pytest.raises(TokenError) as full:
        await _create(db_session, client)
    assert full.value.status == 429


async def test_client_disabled_before_run_is_refused_in_private_chat(db_engine, db_session):
    _, _, client = await _setup(db_session)
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    client.scopes = []
    await db_session.commit()
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    finished = await tasks.get(db_session, task.id)
    assert finished.status == "failed" and finished.error_code == "api_unavailable"
    refused = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:refused")
    )
    assert refused.target == {"chat_id": "p2p-alice"} and "业务系统" in refused.payload["markdown"]


async def test_follow_up_after_a_confirmed_write_runs_in_the_same_session(db_engine, db_session):
    _, _, client = await _setup(db_session)
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    first = await _claim(db_session)
    await _run(db_engine, first)
    # 确认卡点完后平台按原任务续接（同 confirm.resume）：同一会话、同一条入站事件。
    follow = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=first.bot_id,
            kind="chat",
            user_id=first.user_id,
            session_key=first.session_key,
            inbound_event_id=first.inbound_event_id,
            dedupe_key=f"systems-write:{row.id}:resume",
            payload={
                **first.payload,
                "systems_write_result": "用户已确认：1. stock.createDraft：成功",
            },
        ),
    )
    await db_session.commit()
    assert follow is not None
    task = await _claim(db_session)
    assert task.id == follow.id
    fake = await _run(db_engine, task)
    assert len(fake.requests) == 1
    assert "stock.createDraft：成功" in turn_block(fake.requests[0])
    done = await tasks.get(db_session, follow.id)
    assert done.status == "succeeded"
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == follow.id))
    assert stream.reply_context["chat_id"] == "p2p-alice"


async def test_a_foreign_task_naming_the_api_task_is_not_run(db_engine, db_session):
    _, _, client = await _setup(db_session)
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    first = await _claim(db_session)
    await _run(db_engine, first)
    stray = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=first.bot_id,
            kind="chat",
            user_id=first.user_id,
            session_key="p2p-alice",
            inbound_event_id=first.inbound_event_id,
            dedupe_key="stray",
            payload=dict(first.payload),
        ),
    )
    await db_session.commit()
    assert stray is not None
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    assert (await tasks.get(db_session, stray.id)).error_code == "api_inactive"
