"""业务系统代员工发起的任务里的写操作：确认卡发到员工私聊，只有本人在私聊里点才执行。"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from coreman.core.api_tasks import service
from coreman.core.db.models import ApiClient, ApiTask, OutboxItem, Task, UserReached
from coreman.core.systems_catalog import confirm
from tests.api.test_systems_write_confirm import (  # noqa: F401  fixture
    REFUND,
    batch,
    draft,
    fake,
    outcomes,
    posts,
    press,
    release,
    resumed,
    run_click,
    stage,
    turn,
)


async def api_turn(  # type: ignore[no-untyped-def]
    db_session, app, *, client_task=True, silent=False, allowed_writes=None
):
    """把夹具里的私聊一轮改成业务系统代这位员工发起的 API 任务。"""
    bot, user, task = await turn(db_session, app)
    if await db_session.get(UserReached, (bot.id, user.id)) is None:
        db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="oc_private"))
    db_session.add(
        ApiClient(
            app_key="intake",
            name="资料录入",
            scopes=["tasks"],
            bot_ids=[bot.id],
            secret_enc=app.state.cipher.encrypt("client-secret", "api_clients.secret_enc"),
        )
    )
    await db_session.flush()
    row = ApiTask(
        id=uuid.uuid4(),
        client_key="intake",
        user_id=user.id,
        bot_id=bot.id,
        prompt="识别截图并写回",
        target="p2p",
        chat_id="oc_private",
        task_id=task.id,
        silent=silent,
        allowed_writes=allowed_writes,
    )
    db_session.add(row)
    payload = {**task.payload, "api_task_id": str(row.id), "origin": "api"}
    if silent:
        payload[service.SILENT_KEY] = True
    if client_task:
        payload["api_client"] = "intake"
        task.session_key = service.client_session_key(row.id)
    task.payload = payload
    await db_session.commit()
    return bot, user, task, row


async def test_card_goes_to_the_private_chat_and_runs_once_the_member_confirms(
    client,
    app,
    db_session,
    db_engine,
    fake,  # noqa: F811
) -> None:
    bot, user, task, row = await api_turn(db_session, app)
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["status"] == "pending_confirmation" and posts(fake) == []
    writes = (await service.view(db_session, row))["writes"]
    assert writes == [{"system": "stock", "operation": "createDraft", "status": "pending_confirm"}]

    assert await release(app, task)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).one()
    assert card.target == {"chat_id": "oc_private"}
    state = await batch(db_session)
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_done" and posts(fake) == ["/api/stock/documents"]
    row_id = row.id
    db_session.expire_all()  # 点击在 worker 自己的会话里改了批次
    row = await db_session.get(ApiTask, row_id)
    writes = (await service.view(db_session, row))["writes"]
    assert writes == [{"system": "stock", "operation": "createDraft", "status": "succeeded"}]
    # 续接留在这个任务的独立会话里，仍按业务系统发起的任务处理。
    (follow,) = await resumed(db_session)
    assert follow.session_key == f"api:{row_id}"
    assert follow.payload["api_task_id"] == str(row_id) and follow.payload["api_client"] == "intake"
    assert "成功" in follow.payload[confirm.RESULT_KEY]


async def test_only_the_member_in_the_private_chat_can_answer(
    client,
    app,
    db_session,
    db_engine,
    fake,  # noqa: F811
) -> None:
    bot, user, task, _ = await api_turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"))
    assert await release(app, task)
    state = await batch(db_session)
    other = await press(db_session, bot, task, state.id, user_id="someone", open_id="ou_someone")
    assert await run_click(db_session, db_engine, other) == "ignored"
    assert posts(fake) == []
    cancelled = await press(db_session, bot, task, state.id, "cancel")
    assert await run_click(db_session, db_engine, cancelled) == "systems_write_cancelled"
    assert posts(fake) == []
    db_session.expire_all()
    (row_task,) = await db_session.scalars(select(ApiTask))
    writes = (await service.view(db_session, row_task))["writes"]
    assert [w["status"] for w in writes] == ["rejected"]


async def test_personal_token_api_turns_still_stage_nothing(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, user, task, _ = await api_turn(db_session, app, client_task=False)
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["error"] == "confirmation_unavailable"


async def test_a_task_in_someone_elses_session_is_not_an_api_client_turn(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, user, task, _ = await api_turn(db_session, app)
    # 载荷说是业务系统发起的，但会话键不是这个任务的独立会话：不认。
    stored = await db_session.get(Task, task.id)
    assert stored is not None
    stored.session_key = "oc_private"
    await db_session.commit()
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["error"] == "confirmation_unavailable"


async def test_silent_task_runs_listed_writes_at_once_without_a_card(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, user, task, row = await api_turn(
        db_session, app, silent=True, allowed_writes=["stock.createDraft"]
    )
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    # 当场以员工身份执行，结果直接回给 AI 员工；没有暂存，也没有确认卡。
    assert out.get("ok") is True, out
    assert posts(fake) == ["/api/stock/documents"]
    assert not await release(app, task)
    assert list(await db_session.scalars(select(OutboxItem))) == []
    assert await outcomes(db_session) == [("createDraft", "silent_ok")]
    writes = (await service.view(db_session, row))["writes"]
    assert writes == [
        {"system": "stock", "operation": "createDraft", "status": "succeeded", "silent": True}
    ]


async def test_silent_task_refuses_writes_it_did_not_list(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, user, task, row = await api_turn(
        db_session, app, silent=True, allowed_writes=["stock.setPrice"]
    )
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["error"] == "write_not_allowed_by_caller"
    assert posts(fake) == [] and not await release(app, task)
    assert await outcomes(db_session) == [("createDraft", "not_allowed")]
    writes = (await service.view(db_session, row))["writes"]
    assert writes == [{"system": "stock", "operation": "createDraft", "status": "not_allowed"}]


async def test_allowed_writes_limit_a_normal_task_whose_listed_writes_still_need_a_card(
    client,
    app,
    db_session,
    fake,  # noqa: F811
) -> None:
    _, user, task, row = await api_turn(db_session, app, allowed_writes=["stock.createDraft"])
    staged, refused = await stage(client, app, task, user, draft("SKU-1"), REFUND)
    assert staged["status"] == "pending_confirmation"
    assert refused["error"] == "write_not_allowed_by_caller"
    assert posts(fake) == []
    writes = (await service.view(db_session, row))["writes"]
    assert [(w["operation"], w["status"]) for w in writes] == [
        ("createDraft", "pending_confirm"),
        ("refundSupplier", "not_allowed"),
    ]
    assert await release(app, task)
