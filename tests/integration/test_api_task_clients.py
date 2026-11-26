"""业务系统代员工发起 API 任务：按邮箱前缀认人、独立会话、私聊推送与尾注、开轮前复核。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from coreman.core.api_tasks import service
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.bus import tasks
from coreman.core.db.models import (
    ApiClient,
    ApiTask,
    ChatLog,
    ChatSession,
    InboundEvent,
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
    await _create(db_session, client, prompt="第一件")
    await _create(db_session, client, prompt="第二件")
    with pytest.raises(TokenError) as full:
        await _create(db_session, client, prompt="第三件")
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


async def _images(session, client, member, count):  # type: ignore[no-untyped-def]
    from coreman.core.api_tasks import uploads

    ids = []
    for index in range(count):
        data = b"\x89PNG\r\n\x1a\n" + index.to_bytes(4, "big") * 8
        row = await uploads.save(session, client_key=client.app_key, user_id=member.id, data=data)
        ids.append(row.id)
    await session.commit()
    return ids


def _content(body):  # type: ignore[no-untyped-def]
    content = body["messages"][1]["content"]
    assert isinstance(content, list)
    return content


async def test_images_are_given_to_the_model(db_engine, db_session):
    _, member, client = await _setup(db_session)
    ids = await _images(db_session, client, member, 2)
    row, _ = await _create(db_session, client, images=ids)
    await _trigger_sent(db_session, row)
    fake = await _run(db_engine, await _claim(db_session))
    content = _content(fake.requests[0])
    images = [p for p in content if p.get("type") == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "识别截图里的账号信息" in str(content)


async def test_images_past_the_inline_limit_become_download_links(db_engine, db_session):
    from coreman.core.api_tasks import uploads

    _, member, client = await _setup(db_session)
    ids = await _images(db_session, client, member, uploads.INLINE + 2)
    row, _ = await _create(db_session, client, images=ids)
    await _trigger_sent(db_session, row)
    fake = await _run(db_engine, await _claim(db_session))
    content = _content(fake.requests[0])
    assert len([p for p in content if p.get("type") == "image_url"]) == uploads.INLINE
    text = "\n".join(str(p.get("text") or "") for p in content)
    assert f"本次共 {uploads.INLINE + 2} 张图片" in text
    assert text.count("/api/infra/tasks/uploads/") == 2
    assert "链接 24 小时内有效" in text
    assert str(ids[-1]) in text and str(ids[0]) not in text


async def _run_with_bucket(engine, task):  # type: ignore[no-untyped-def]
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.approval_helpers import STORE
    from tests.integration.worker_helpers import build_ctx

    fake = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: fake.client())
    ctx.tmp_store = STORE
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    return fake


async def test_images_past_the_inline_limit_go_through_the_temporary_bucket(
    db_engine, db_session, monkeypatch
):
    from coreman.core.api_tasks import uploads
    from coreman.core.db.models import ApiTaskUpload
    from tests.integration.approval_helpers import install_files

    _, bucket = install_files(monkeypatch)
    _, member, client = await _setup(db_session)
    ids = await _images(db_session, client, member, uploads.INLINE + 2)
    row, _ = await _create(db_session, client, images=ids)
    await _trigger_sent(db_session, row)
    fake = await _run_with_bucket(db_engine, await _claim(db_session))
    text = "\n".join(str(p.get("text") or "") for p in _content(fake.requests[0]))
    assert "链接 7 天内有效" in text
    assert "/api/infra/tasks/uploads/" not in text
    assert text.count("https://tmp.example.com/pa/api-tasks/") == 2
    assert len(bucket.objects) == 2
    last = await db_session.get(ApiTaskUpload, ids[-1])
    assert bytes(last.data) in bucket.objects.values()


async def test_temporary_bucket_down_falls_back_to_platform_links(
    db_engine, db_session, monkeypatch
):
    from coreman.core.api_tasks import uploads
    from tests.integration.approval_helpers import install_files

    _, bucket = install_files(monkeypatch)
    bucket.down = True
    _, member, client = await _setup(db_session)
    ids = await _images(db_session, client, member, uploads.INLINE + 1)
    row, _ = await _create(db_session, client, images=ids)
    await _trigger_sent(db_session, row)
    fake = await _run_with_bucket(db_engine, await _claim(db_session))
    text = "\n".join(str(p.get("text") or "") for p in _content(fake.requests[0]))
    assert "链接 24 小时内有效" in text
    assert text.count("/api/infra/tasks/uploads/") == 1


async def test_known_exhausted_quota_waits_without_running_or_messaging(db_engine, db_session):
    from datetime import timedelta

    from coreman.core.db.models import Bot, RelayServer
    from coreman.core.timeutils import utcnow

    bot, _, client = await _setup(db_session)
    relay = await db_session.get(RelayServer, (await db_session.get(Bot, bot.id)).relay_server_id)
    resets = utcnow() + timedelta(hours=2)
    relay.rate_limit_5h_used_pct, relay.rate_limit_5h_resets_at = 100, resets
    await db_session.commit()
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    waiting = await tasks.get(db_session, task.id)
    assert waiting.status == "queued" and waiting.run_after >= resets - timedelta(seconds=5)
    view = await service.view(
        db_session, await db_session.get(ApiTask, row.id, populate_existing=True)
    )
    assert view["status"] == "waiting_quota" and view["resume_at"]
    sent = await db_session.scalars(
        select(OutboxItem.dedupe_key).where(OutboxItem.dedupe_key.like(f"api-task:{row.id}:%"))
    )
    assert set(sent) == {f"api-task:{row.id}:trigger"}


async def test_waiting_longer_than_a_week_gives_up(db_engine, db_session):
    from datetime import timedelta

    from coreman.core.db.models import Bot, RelayServer
    from coreman.core.timeutils import utcnow

    bot, _, client = await _setup(db_session)
    relay = await db_session.get(RelayServer, (await db_session.get(Bot, bot.id)).relay_server_id)
    relay.rate_limit_7d_used_pct = 100
    relay.rate_limit_7d_resets_at = utcnow() + timedelta(days=1)
    await db_session.commit()
    row, _ = await _create(db_session, client)
    stored = await db_session.get(ApiTask, row.id)
    stored.quota_waiting_since = utcnow() - timedelta(days=8)
    await db_session.commit()
    await _trigger_sent(db_session, row)
    task = await _claim(db_session)
    await _run(db_engine, task)
    done = await tasks.get(db_session, task.id)
    assert done.status == "failed" and done.error_code == "quota_wait_expired"


async def test_hitting_the_limit_mid_turn_queues_a_retry_in_the_same_session(db_engine, db_session):
    from tests.fakes.fake_relay import FakeRelay

    _, _, client = await _setup(db_session)
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    first = await _claim(db_session)
    await _run(db_engine, first, FakeRelay("rate_limit"))
    stored = await db_session.get(ApiTask, row.id, populate_existing=True)
    assert stored.task_id != first.id and stored.resume_at is not None
    retry = await tasks.get(db_session, stored.task_id)
    assert retry.session_key == first.session_key and retry.status == "queued"
    assert retry.inbound_event_id == first.inbound_event_id
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == first.id))
    assert "已排队" in log.response_content and "切换" not in log.response_content
    view = await service.view(db_session, stored)
    assert view["status"] == "waiting_quota"


async def test_same_content_while_unfinished_returns_the_same_task(db_session):
    _, _, client = await _setup(db_session)
    first, created = await _create(db_session, client)
    again, again_created = await _create(db_session, client)
    assert created and not again_created and again.id == first.id
    other, other_created = await _create(db_session, client, prompt="另一件事")
    assert other_created and other.id != first.id
    await service.cancel(db_session, first)
    await db_session.commit()
    third, third_created = await _create(db_session, client)
    assert third_created and third.id != first.id


async def _quote(db_session, bot, parent_id, text="粉丝数不对，应该是 12 万"):  # type: ignore[no-untyped-def]
    from tests.integration.test_chat_handler import chat_task

    task = await chat_task(db_session, bot, text, sender="ou_alice", chat_id="p2p-alice")
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    event.reply_context = {
        **event.reply_context,
        "chat_id": "p2p-alice",
        "message_id": event.platform_msg_id,
        "parent_id": parent_id,
    }
    await db_session.commit()
    return task


async def _finished_api_task(db_engine, db_session, client, member, images=0):  # type: ignore[no-untyped-def]
    from coreman.core.db.models import FeishuDelivery

    ids = await _images(db_session, client, member, images) if images else []
    row, _ = await _create(db_session, client, images=ids)
    await _trigger_sent(db_session, row)
    first = await _claim(db_session)
    await _run(db_engine, first)
    db_session.add(FeishuDelivery(task_id=first.id, message_id="om_result"))
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    trigger.payload = {**trigger.payload, "_feishu_message_id": "om_trigger"}
    await db_session.commit()
    return row, ids


async def test_quoting_the_result_brings_the_original_task_in_once(db_engine, db_session):
    from coreman.core.db.models import Bot

    bot_row, member, client = await _setup(db_session)
    bot = await db_session.get(Bot, bot_row.id)
    row, ids = await _finished_api_task(db_engine, db_session, client, member, images=2)
    fake = await _run(db_engine, await _quote(db_session, bot, "om_result"))
    content = fake.requests[0]["messages"][1]["content"]
    assert isinstance(content, list)
    text = "\n".join(str(p.get("text") or "") for p in content)
    assert "业务系统「资料录入」里发起" in text and "识别截图里的账号信息" in text
    assert "原任务的回复" in text and "你好，世界" in text
    assert len([p for p in content if p.get("type") == "image_url"]) == 2
    # 同一个会话里再引用（这次引用「在某系统里发起」那句）不再重复带。
    again = await _run(db_engine, await _quote(db_session, bot, "om_trigger", "再看一下"))
    assert "业务系统「资料录入」里发起" not in str(again.requests[0]["messages"][1]["content"])


async def test_recall_only_for_the_member_in_their_own_chat(db_engine, db_session):
    from coreman.core.db.models import Bot

    bot_row, member, client = await _setup(db_session)
    bot = await db_session.get(Bot, bot_row.id)
    await _finished_api_task(db_engine, db_session, client, member)
    stranger = User(login_name="u-eve", display_name="伊芙", email="eve@example.test")
    db_session.add(stranger)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=stranger.id, platform="feishu", platform_user_id="ou_eve"))
    await db_session.commit()
    from tests.integration.test_chat_handler import chat_task

    task = await chat_task(db_session, bot, "这是什么", sender="ou_eve", chat_id="p2p-alice")
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    event.reply_context = {**event.reply_context, "chat_id": "p2p-alice", "parent_id": "om_result"}
    await db_session.commit()
    fake = await _run(db_engine, task)
    assert "业务系统「资料录入」里发起" not in str(fake.requests[0]["messages"][1]["content"])


async def _silent_setup(session):  # type: ignore[no-untyped-def]
    bot, member, client = await _setup(session)
    client.silent_writes = ["profile_admin.saveDraft", "profile_admin.saveAudit"]
    await session.commit()
    return bot, member, client


async def test_silent_needs_allowed_writes(db_session):
    _, _, client = await _silent_setup(db_session)
    with pytest.raises(TokenError) as missing:
        await _create(db_session, client, silent=True)
    assert missing.value.status == 422 and "allowed_writes" in missing.value.message
    with pytest.raises(TokenError) as empty:
        await _create(db_session, client, allowed_writes=[])
    assert empty.value.status == 422
    with pytest.raises(TokenError) as malformed:
        await _create(db_session, client, silent=True, allowed_writes=["saveDraft"])
    assert malformed.value.status == 422 and "saveDraft" in malformed.value.message
    # 非静默时 allowed_writes 只是收窄，不要求登记过。
    row, created = await _create(db_session, client, allowed_writes=["profile_admin.updatePayout"])
    assert created and not row.silent and row.allowed_writes == ["profile_admin.updatePayout"]


async def test_silent_and_allowed_writes_count_as_content_for_request_id(db_session):
    _, _, client = await _silent_setup(db_session)
    first, created = await _create(
        db_session,
        client,
        request_id="lead-1",
        silent=True,
        allowed_writes=["profile_admin.saveDraft", "profile_admin.saveAudit"],
    )
    # 顺序不同、重复的项不算改了内容。
    again, replayed = await _create(
        db_session,
        client,
        request_id="lead-1",
        silent=True,
        allowed_writes=[
            "profile_admin.saveAudit",
            "profile_admin.saveDraft",
            "profile_admin.saveDraft",
        ],
    )
    assert created and not replayed and again.id == first.id
    for changed in (
        {"silent": False, "allowed_writes": ["profile_admin.saveDraft", "profile_admin.saveAudit"]},
        {"silent": True, "allowed_writes": ["profile_admin.saveDraft"]},
    ):
        with pytest.raises(TokenError) as conflict:
            await _create(db_session, client, request_id="lead-1", **changed)
        assert conflict.value.status == 409
    # 不带 request_id 时同内容去重同样看这两项：换成非静默就是另一件事。
    loud, made = await _create(db_session, client)
    assert made and loud.id != first.id


async def test_silent_task_sends_nothing_to_the_private_chat(db_engine, db_session):
    _, _, client = await _silent_setup(db_session)
    row, _ = await _create(
        db_session, client, silent=True, allowed_writes=["profile_admin.saveDraft"]
    )
    # 连「在某系统里发起」也不发；没有触发消息要等，直接开轮。
    assert list(await db_session.scalars(select(OutboxItem))) == []
    task = await _claim(db_session)
    assert task.payload[service.SILENT_KEY] is True
    fake = await _run(db_engine, task)
    block = turn_block(fake.requests[0])
    assert "静默执行" in block and "profile_admin.saveDraft" in block
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    assert stream.reply_context.get("_silent") is True
    assert list(await db_session.scalars(select(OutboxItem))) == []
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert "不在当前对话上下文中" not in log.response_content
    view = await service.view(db_session, row)
    assert view["status"] == "succeeded" and "你好，世界" in view["reply"]
    assert view["silent"] is True and view["allowed_writes"] == ["profile_admin.saveDraft"]


async def test_silent_with_unregistered_writes_falls_back_to_confirmation(db_engine, db_session):
    _, _, client = await _silent_setup(db_session)
    wanted = ["profile_admin.saveDraft", "profile_admin.updatePayout"]
    row, created = await _create(
        db_session, client, request_id="lead-2", silent=True, allowed_writes=wanted
    )
    # 整个任务按普通模式跑：不按项拆成部分静默；写操作范围照常收窄。
    assert created and not row.silent and row.silent_unregistered == ["profile_admin.updatePayout"]
    assert row.allowed_writes == wanted
    assert len(list(await db_session.scalars(select(OutboxItem)))) == 1
    task = await _claim(db_session)
    assert service.SILENT_KEY not in task.payload
    view = await service.view(db_session, row)
    assert view["silent"] is False
    assert view["silent_fallback"] == {
        "reason": "not_registered",
        "unregistered": ["profile_admin.updatePayout"],
    }
    # 幂等按请求内容比较：请求里是 silent=true，重放还按 true 比。
    again, replayed = await _create(
        db_session, client, request_id="lead-2", silent=True, allowed_writes=wanted
    )
    assert not replayed and again.id == row.id
    with pytest.raises(TokenError) as conflict:
        await _create(db_session, client, request_id="lead-2", allowed_writes=wanted)
    assert conflict.value.status == 409
    # 没配静默清单的调用方同样回退。
    client.silent_writes = []
    await db_session.commit()
    bare, made = await _create(
        db_session, client, prompt="另一件", silent=True, allowed_writes=["profile_admin.saveDraft"]
    )
    assert made and not bare.silent and bare.silent_unregistered == ["profile_admin.saveDraft"]
    # 静默生效的任务没有回退原因。
    client.silent_writes = ["profile_admin.saveDraft"]
    await db_session.commit()
    quiet, _ = await _create(
        db_session, client, prompt="第三件", silent=True, allowed_writes=["profile_admin.saveDraft"]
    )
    assert quiet.silent and quiet.silent_unregistered is None
    assert (await service.view(db_session, quiet))["silent_fallback"] is None


async def test_silent_task_that_asks_the_member_fails_instead_of_waiting(db_engine, db_session):
    from tests.fakes.fake_relay import FakeRelay

    _, _, client = await _silent_setup(db_session)
    row, _ = await _create(
        db_session, client, silent=True, allowed_writes=["profile_admin.saveDraft"]
    )
    task = await _claim(db_session)
    await _run(db_engine, task, FakeRelay("ask_user"))
    assert list(await db_session.scalars(select(OutboxItem))) == []
    view = await service.view(db_session, row)
    assert view["status"] == "failed" and view["error_code"] == service.SILENT_ASK_CODE
    assert "用哪个" in view["reply"]


async def test_silent_task_refused_before_run_says_nothing_in_private_chat(db_engine, db_session):
    _, _, client = await _silent_setup(db_session)
    row, _ = await _create(
        db_session, client, silent=True, allowed_writes=["profile_admin.saveDraft"]
    )
    client.scopes = []
    await db_session.commit()
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    assert list(await db_session.scalars(select(OutboxItem))) == []
    view = await service.view(db_session, row)
    assert view["status"] == "failed" and view["error_code"] == "api_unavailable"
