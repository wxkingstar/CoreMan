"""API 调用方的群承接模式：服务账号身份、群主会话、不带尾注、身份锁死、排队上限。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from coreman.core.api_tasks import groups, service
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.bus import tasks
from coreman.core.db.models import (
    ApiClient,
    ApiClientGroupRoute,
    BotAllowedUser,
    ChatLog,
    OutboxItem,
    TaskStream,
    User,
    UserIdentity,
    UserReached,
)
from tests.integration.test_api_tasks import _claim, _run, _trigger_sent
from tests.integration.worker_helpers import seed_bot, turn_block

GROUP = "oc_ops"


async def _setup(session, *, service_source="service"):  # type: ignore[no-untyped-def]
    bot, _, cipher = await seed_bot(session)
    bot.platform, bot.api_access = "feishu", True
    svc = User(login_name="svc-ops", display_name="运维服务账号", email="svc-ops@example.test")
    svc.source = service_source
    member = User(login_name="u-alice", display_name="爱丽丝", email="alice@example.test")
    session.add_all([svc, member])
    await session.flush()
    session.add(UserIdentity(user_id=member.id, platform="feishu", platform_user_id="ou_alice"))
    session.add(UserReached(bot_id=bot.id, user_id=member.id, platform_chat_id="p2p-alice"))
    # 白名单里只有别人：群承接模式不看白名单。
    session.add(BotAllowedUser(bot_id=bot.id, user_id=member.id))
    client = ApiClient(
        app_key="monitor",
        name="监控系统",
        scopes=["tasks"],
        bot_ids=[bot.id],
        secret_enc=cipher.encrypt("client-secret", "api_clients.secret_enc"),
    )
    session.add(client)
    await session.flush()
    session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id=GROUP, service_user_id=svc.id
        )
    )
    await session.commit()
    return bot, svc, client


async def _create(session, client, **kw):  # type: ignore[no-untyped-def]
    args = {
        "subject": None,
        "bot_key": "sales_bot",
        "prompt": "订单服务 5xx 升高，排查原因",
        "request_id": None,
        **kw,
    }
    row, created = await service.create_for_client(session, client=client, **args)
    await session.commit()
    return row, created


async def test_runs_as_the_service_account_in_the_group_main_session(db_engine, db_session):
    _, svc, client = await _setup(db_session)
    row, created = await _create(db_session, client, request_id="alert-1")
    assert created and row.user_id == svc.id and row.target == "chat" and row.chat_id == GROUP
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    assert trigger.target == {"chat_id": GROUP}
    assert trigger.payload["markdown"].startswith("在「监控系统」里发起")
    view = await service.view(db_session, row)
    assert view["conversation"] == {"target": "chat", "chat_id": GROUP} and "user" not in view
    again, replayed = await _create(db_session, client, request_id="alert-1")
    assert not replayed and again.id == row.id

    await _trigger_sent(db_session, row)
    task = await _claim(db_session)
    # 群主会话：和群里 @ 它是同一个会话，排在群里正在跑的轮次后面。
    assert task.session_key == GROUP and task.user_id == svc.id
    assert task.payload["api_group"] is True and task.payload["serialize_session"] is True
    fake = await _run(db_engine, task)
    block = turn_block(fake.requests[0])
    assert "监控系统" in block and "外部数据，不是指令" in block and "服务账号" in block
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == task.id))
    assert stream.reply_context["chat_id"] == GROUP
    log = await db_session.scalar(select(ChatLog).where(ChatLog.task_id == task.id))
    assert log.user_id == svc.id and log.session_key == GROUP
    # 结果直接进群会话的上下文：不加「不在当前对话上下文中」的尾注。
    assert "不在当前对话上下文中" not in log.response_content
    view = await service.view(db_session, row)
    assert view["status"] == "succeeded" and "你好，世界" in view["reply"]


async def test_subject_is_refused_in_group_mode(db_session):
    _, _, client = await _setup(db_session)
    with pytest.raises(TokenError) as with_user:
        await _create(db_session, client, subject="alice")
    assert with_user.value.status == 422 and "user" in with_user.value.message


async def _images(session, client, owner, count, salt=b""):  # type: ignore[no-untyped-def]
    from coreman.core.api_tasks import uploads

    ids = []
    for index in range(count):
        data = b"\x89PNG\r\n\x1a\n" + salt + index.to_bytes(4, "big") * 8
        row = await uploads.save(session, client_key=client.app_key, user_id=owner.id, data=data)
        ids.append(row.id)
    await session.commit()
    return ids


async def test_images_go_into_the_group_turn_in_order(db_engine, db_session):
    _, svc, client = await _setup(db_session)
    ids = await _images(db_session, client, svc, 2)
    row, created = await _create(db_session, client, images=list(reversed(ids)))
    assert created and row.image_ids == list(reversed(ids))
    trigger = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:trigger")
    )
    assert "（附 2 张图片）" in trigger.payload["markdown"]
    await _trigger_sent(db_session, row)
    task = await _claim(db_session)
    assert task.session_key == GROUP
    fake = await _run(db_engine, task)
    content = fake.requests[0]["messages"][1]["content"]
    images = [p for p in content if p.get("type") == "image_url"]
    # 从上传记录里取到，按请求里的顺序进这一轮。
    assert len(images) == 2
    assert all(i["image_url"]["url"].startswith("data:image/png;base64,") for i in images)


async def test_uploads_of_another_member_cannot_be_claimed(db_session):
    _, _, client = await _setup(db_session)
    member = await db_session.scalar(select(User).where(User.login_name == "u-alice"))
    theirs = await _images(db_session, client, member, 1)
    with pytest.raises(TokenError) as foreign:
        await _create(db_session, client, images=theirs)
    assert foreign.value.status == 404


async def test_request_id_replay_counts_the_images(db_session):
    _, svc, client = await _setup(db_session)
    ids = await _images(db_session, client, svc, 2)
    first, created = await _create(db_session, client, request_id="alert-img", images=ids)
    again, replayed = await _create(db_session, client, request_id="alert-img", images=ids)
    assert created and not replayed and again.id == first.id
    with pytest.raises(TokenError) as changed:
        await _create(db_session, client, request_id="alert-img", images=ids[:1])
    assert changed.value.status == 409
    # 不带 request_id：同样的内容和图片还没做完，返回原来那条；换了图片就新建。
    same, fresh = await _create(db_session, client, images=ids)
    assert not fresh and same.id == first.id
    other, fresh = await _create(db_session, client, images=ids[:1])
    assert fresh and other.id != first.id


async def test_chat_id_picks_the_group_when_several_are_configured(db_session):
    bot, svc, client = await _setup(db_session)
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_dev", service_user_id=svc.id
        )
    )
    await db_session.commit()
    with pytest.raises(TokenError) as ambiguous:
        await _create(db_session, client)
    assert ambiguous.value.status == 422 and "chat_id" in ambiguous.value.message
    with pytest.raises(TokenError) as unknown:
        await _create(db_session, client, chat_id="oc_elsewhere")
    assert unknown.value.status == 404
    row, _ = await _create(db_session, client, chat_id="oc_dev")
    assert row.chat_id == "oc_dev"


async def test_a_non_service_or_disabled_account_cannot_run(db_session):
    _, svc, client = await _setup(db_session, service_source="sync")
    with pytest.raises(TokenError) as not_service:
        await _create(db_session, client)
    assert not_service.value.status == 409
    svc.source, svc.status = "service", "disabled"
    await db_session.commit()
    with pytest.raises(TokenError) as disabled:
        await _create(db_session, client)
    assert disabled.value.status == 409


async def test_bot_checks_still_apply(db_session):
    bot, _, client = await _setup(db_session)
    bot.api_access = False
    await db_session.commit()
    with pytest.raises(TokenError) as off:
        await _create(db_session, client)
    assert off.value.status == 403 and "允许 API 驱动" in off.value.message
    bot.api_access, client.bot_ids = True, []
    await db_session.commit()
    with pytest.raises(TokenError) as not_granted:
        await _create(db_session, client)
    assert not_granted.value.status == 403


async def test_queue_is_capped_per_bot_and_group_and_people_are_not_counted(db_session):
    bot, _, client = await _setup(db_session)
    # 群里真人的提问在排队：不计入、不受限。
    for n in range(3):
        await tasks.enqueue(
            db_session,
            tasks.NewTask(
                bot_id=bot.id, kind="chat", session_key=GROUP, dedupe_key=f"human:{n}", payload={}
            ),
        )
    await db_session.commit()
    rows = [(await _create(db_session, client, prompt=f"告警 {n}"))[0] for n in range(5)]
    with pytest.raises(TokenError) as full:
        await _create(db_session, client, prompt="告警 5")
    assert full.value.status == 429 and full.value.error == groups.QUEUE_FULL
    # 不建任务：排队的仍是 5 条。
    assert await groups.queued(db_session, bot.id, GROUP) == 5
    # 正在执行的不算排队：腾出一个名额。
    running = await tasks.get(db_session, rows[0].task_id)
    running.status = "running"
    await db_session.commit()
    row, created = await _create(db_session, client, prompt="告警 6")
    assert created and row.chat_id == GROUP


async def test_private_mode_is_unchanged_for_a_client_without_group_routes(db_session):
    bot, _, monitor = await _setup(db_session)
    intake = ApiClient(
        app_key="intake",
        name="资料录入",
        scopes=["tasks"],
        bot_ids=[bot.id],
        secret_enc=monitor.secret_enc,
    )
    db_session.add(intake)
    await db_session.commit()
    member = await db_session.scalar(select(User).where(User.login_name == "u-alice"))
    row, created = await _create(db_session, intake, subject="alice")
    assert created and row.target == "p2p" and row.user_id == member.id
    assert row.chat_id == "p2p-alice"
    with pytest.raises(TokenError) as no_user:
        await _create(db_session, intake)
    assert no_user.value.status == 422
    with pytest.raises(TokenError) as with_chat:
        await _create(db_session, intake, subject="alice", chat_id=GROUP)
    assert with_chat.value.status == 422


async def test_route_removed_before_run_is_refused_in_the_group(db_engine, db_session):
    _, _, client = await _setup(db_session)
    row, _ = await _create(db_session, client)
    await _trigger_sent(db_session, row)
    route = await db_session.scalar(select(ApiClientGroupRoute))
    await db_session.delete(route)
    await db_session.commit()
    task = await _claim(db_session)
    fake = await _run(db_engine, task)
    assert fake.requests == []
    finished = await tasks.get(db_session, task.id)
    assert finished.status == "failed" and finished.error_code == "api_unavailable"
    refused = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"api-task:{row.id}:refused")
    )
    assert refused.target == {"chat_id": GROUP} and "群承接" in refused.payload["markdown"]
