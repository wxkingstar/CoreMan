"""群承接模式的写操作：确认卡发到群里，群成员中在使用白名单里的人能点，按点卡人身份执行。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from coreman.core.api_tasks import service
from coreman.core.db.models import (
    ApiClient,
    ApiClientGroupRoute,
    ApiTask,
    BotAllowedUser,
    OutboxItem,
    SystemCall,
    User,
    UserIdentity,
)
from tests.api.test_systems_write_confirm import (  # noqa: F401  fixture
    batch,
    draft,
    fake,
    posts,
    press,
    release,
    resumed,
    run_click,
    stage,
    turn,
)

MEMBERS = {"clicker", "late", "outsider-whitelist"}


@pytest.fixture
def group_members(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """飞书群成员查询换成本地名单：名单里的人算在群里。"""
    members = set(MEMBERS)

    async def in_group(bot, chat_id, platform_user_id, cipher):  # type: ignore[no-untyped-def]
        return chat_id == "oc_group" and platform_user_id in members

    monkeypatch.setattr("coreman.core.chat.human_collaboration.in_group", in_group)
    return members


async def _member(db_session, login: str, pid: str) -> User:  # type: ignore[no-untyped-def]
    user = User(login_name=login, display_name=login, email=f"{login}@example.test")
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=user.id, platform="feishu", platform_user_id=pid, open_id=f"ou_{pid}")
    )
    return user


async def group_turn(db_session, app):  # type: ignore[no-untyped-def]
    """把夹具里的群聊一轮改成监控系统以服务账号在群里发起的 API 任务。"""
    bot, _, task = await turn(db_session, app, "group")
    bot.api_access = True
    svc = User(login_name="svc-ops", display_name="运维服务账号", email="svc-ops@example.test")
    svc.source = "service"
    db_session.add(svc)
    clicker = await _member(db_session, "clicker", "clicker")
    late = await _member(db_session, "late", "late")
    db_session.add(
        ApiClient(
            app_key="monitor",
            name="监控系统",
            scopes=["tasks"],
            bot_ids=[bot.id],
            secret_enc=app.state.cipher.encrypt("client-secret", "api_clients.secret_enc"),
        )
    )
    await db_session.flush()
    db_session.add(
        ApiClientGroupRoute(
            client_key="monitor", bot_id=bot.id, chat_id="oc_group", service_user_id=svc.id
        )
    )
    row = ApiTask(
        id=uuid.uuid4(),
        client_key="monitor",
        user_id=svc.id,
        bot_id=bot.id,
        prompt="排查订单服务 5xx",
        target="chat",
        chat_id="oc_group",
        task_id=task.id,
    )
    db_session.add(row)
    task.payload = {
        **task.payload,
        "api_task_id": str(row.id),
        "origin": "api",
        "api_client": "monitor",
        "api_group": True,
    }
    task.session_key = "oc_group"
    task.user_id = svc.id
    await db_session.commit()
    return bot, svc, clicker, late, task, row


async def _staged(client, app, db_session, task, svc):  # type: ignore[no-untyped-def]
    (out,) = await stage(client, app, task, svc, draft("SKU-1"))
    assert out["status"] == "pending_confirmation", out
    assert await release(app, task)
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert card.target == {"chat_id": "oc_group"}
    return await batch(db_session)


async def test_card_goes_to_the_group_and_runs_as_whoever_confirms(
    client,
    app,
    db_session,
    db_engine,
    fake,  # noqa: F811
    group_members,
) -> None:
    bot, svc, clicker, _, task, row = await group_turn(db_session, app)
    clicker_id, row_id = clicker.id, row.id
    state = await _staged(client, app, db_session, task, svc)
    assert state.state["chat_type"] == "group" and state.state["user_id"] == str(svc.id)
    pressed = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="clicker", open_id="ou_clicker"
    )
    assert await run_click(db_session, db_engine, pressed) == "systems_write_done"
    assert posts(fake) == ["/api/stock/documents"]
    db_session.expire_all()
    done = (
        await db_session.scalars(
            select(SystemCall).where(SystemCall.outcome != "proposed").order_by(SystemCall.id)
        )
    ).all()
    # 业务系统调用记录的是真实点卡人，不是服务账号。
    assert done and all(call.user_id == clicker_id for call in done)
    (follow,) = await resumed(db_session)
    assert follow.session_key == "oc_group" and follow.payload["api_group"] is True
    row = await db_session.get(ApiTask, row_id)
    writes = (await service.view(db_session, row))["writes"]
    assert writes == [{"system": "stock", "operation": "createDraft", "status": "succeeded"}]


async def test_a_late_second_click_is_told_it_was_already_answered(
    client,
    app,
    db_session,
    db_engine,
    fake,  # noqa: F811
    group_members,
) -> None:
    bot, svc, _, _, task, _ = await group_turn(db_session, app)
    state = await _staged(client, app, db_session, task, svc)
    first = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="clicker", open_id="ou_clicker"
    )
    assert await run_click(db_session, db_engine, first) == "systems_write_done"
    second = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="late", open_id="ou_late"
    )
    assert await run_click(db_session, db_engine, second) == "ignored"
    assert posts(fake) == ["/api/stock/documents"]
    told = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like("%:card_taken"))
    )
    assert told is not None and "已经有人处理过" in told.payload["markdown"]
    assert told.payload["_mention_user_id"] == "late"


async def test_clickers_outside_the_group_or_whitelist_are_refused(
    client,
    app,
    db_session,
    db_engine,
    fake,  # noqa: F811
    group_members,
) -> None:
    bot, svc, clicker, late, task, _ = await group_turn(db_session, app)
    state = await _staged(client, app, db_session, task, svc)
    # 不在群里：飞书查不到他。
    group_members.discard("late")
    outsider = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="late", open_id="ou_late"
    )
    assert await run_click(db_session, db_engine, outsider) == "ignored"
    # 白名单非空且不含点卡人：在群里也不能点。
    db_session.add(BotAllowedUser(bot_id=bot.id, user_id=late.id))
    await db_session.commit()
    not_allowed = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="clicker", open_id="ou_clicker"
    )
    assert await run_click(db_session, db_engine, not_allowed) == "ignored"
    assert posts(fake) == []
    # 加进白名单就能点。
    db_session.add(BotAllowedUser(bot_id=bot.id, user_id=clicker.id))
    await db_session.commit()
    allowed = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="clicker", open_id="ou_clicker"
    )
    assert await run_click(db_session, db_engine, allowed) == "systems_write_done"
    assert posts(fake) == ["/api/stock/documents"]


async def test_a_human_group_turn_is_not_a_group_delivery(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, svc, _, _, task, _ = await group_turn(db_session, app)
    # 载荷没有群承接标记：不按群承接认，服务账号不是这条群消息的发言人，暂存不了。
    task.payload = {k: v for k, v in task.payload.items() if k != "api_group"}
    await db_session.commit()
    (out,) = await stage(client, app, task, svc, draft("SKU-1"))
    assert out["error"] == "confirmation_unavailable"
