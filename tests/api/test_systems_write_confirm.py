"""Proxied writes wait for the asker's confirmation card, then run exactly as staged."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import (
    BotSystemGrant,
    BusinessSystem,
    InboundEvent,
    InteractionState,
    OutboxItem,
    SystemCall,
    Task,
)
from coreman.core.systems_catalog import confirm
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.api.test_feishu_personal import setup
from tests.api.test_systems_catalog import bearer, call
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.worker_helpers import build_ctx

CHATS = {"single": "oc_private", "group": "oc_group"}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    seen: list[str | None] = []

    def adjust(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key"))
        return httpx.Response(200, json={"code": 0, "data": {"adjusted": True}})

    system.spec["paths"]["/api/stock/adjust"] = {  # type: ignore[index]
        "post": {
            "operationId": "adjustStock",
            "tags": ["documents"],
            "summary": "调整库存",
            "x-permission": "stock:doc:write",
            "parameters": [
                {"name": "Idempotency-Key", "in": "header", "required": True, "schema": {}}
            ],
            "responses": {"200": {"description": "OK"}},
        }
    }
    system.routes[("POST", "/api/stock/adjust")] = adjust
    system.routes[("POST", "/api/stock/documents/D1/cancel")] = lambda request: httpx.Response(
        200, json={"code": 0, "data": {"id": "D1", "status": "cancelled"}}
    )
    system.seen_keys = seen  # type: ignore[attr-defined]
    install(monkeypatch, system)
    return system


async def turn(
    db_session: AsyncSession, app: Any, chat_type: str = "single", *, write: bool = True
) -> tuple[Any, Any, Task]:
    bot, user, task = await setup(db_session, app, chat_type=chat_type)
    user.email = "reader@example.test"
    db_session.add(
        BusinessSystem(
            key="stock",
            name="库存",
            base_url=BASE_URL,
            openapi_url=SPEC_URL,
            token_delivery="proxy",
            allowed_bot_ids=None,
        )
    )
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="stock", allow_write=write))
    await db_session.commit()
    return bot, user, task


def draft(sku: str) -> dict[str, Any]:
    return {"system": "stock", "operation_id": "createDraft", "body": {"sku": sku, "quantity": 1}}


async def stage(client: Any, app: Any, task: Task, user: Any, *calls: dict[str, Any]) -> list[Any]:
    auth = bearer(app, task, user)
    return [await call(client, auth, "systems_call", args) for args in calls]


async def release(app: Any, task: Task, *, send: bool = True) -> bool:
    async with app.state.session_factory() as session:
        sent = await confirm.release(
            session, app.state.cipher, bot_id=task.bot_id, task_id=task.id, send=send
        )
        await session.commit()
    return sent


async def batch(db_session: AsyncSession) -> InteractionState:
    state = await db_session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == confirm.KIND)
        .execution_options(populate_existing=True)
    )
    assert state is not None
    return state


async def press(
    session: AsyncSession,
    bot: Any,
    original: Task,
    state_id: uuid.UUID,
    verb: str = "confirm",
    *,
    chat_type: str = "single",
    user_id: str = "human",
    open_id: str = "ou_human",
) -> Task:
    chat_id = CHATS[chat_type]
    card = await session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "send").order_by(OutboxItem.id.desc())
    )
    assert card is not None
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    action = {
        "task_id": f"personal:{original.id}",
        "event_key": confirm.CARD_EVENT,
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_platform_user_id=user_id,
        sender_open_id=open_id,
        reply_context={"chat_id": chat_id, "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-test",
                },
                "event": {
                    "operator": {"user_id": user_id, "open_id": open_id},
                    "context": {"open_chat_id": chat_id, "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": user_id},
            session_key=chat_id,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await session.commit()
    return task


async def run_click(db_session: AsyncSession, db_engine: Any, pressed: Task) -> str:
    await CardActionHandler().run(build_ctx(db_engine, pressed))
    done = await db_session.get(Task, pressed.id, populate_existing=True)
    assert done is not None and done.result is not None
    return str(done.result["card"])


async def outcomes(db_session: AsyncSession) -> list[tuple[str, str]]:
    rows = await db_session.scalars(select(SystemCall).order_by(SystemCall.id))
    return [(row.operation_id, row.outcome) for row in rows]


async def last_update(db_session: AsyncSession) -> str:
    item = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update").order_by(OutboxItem.id.desc())
    )
    assert item is not None
    return str(item.payload["card"]["body"]["elements"][0]["text"]["content"])


async def resumed(db_session: AsyncSession) -> list[Task]:
    rows = await db_session.scalars(select(Task).where(Task.dedupe_key.like("systems-write:%")))
    return list(rows)


def posts(fake: FakeBusinessSystem) -> list[str]:
    return [r.url.path for r in fake.requests if r.method == "POST"]


async def test_writes_wait_for_one_card_and_run_once_confirmed(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    first, second, again = await stage(
        client, app, task, user, draft("SKU-1"), draft("SKU-2"), draft("SKU-1")
    )
    assert [first["pending"], second["pending"], again["pending"]] == [1, 2, 2]
    assert first["status"] == "pending_confirmation" and "不要说已经完成" in first["hint"]
    assert posts(fake) == []
    state = await batch(db_session)
    # The staged requests are encrypted; only who, where and how many are readable.
    assert "SKU-1" not in str(state.state) and state.state["count"] == 2

    assert await release(app, task)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).one()
    elements = card.payload["card"]["body"]["elements"]
    text = elements[1]["elements"][0]["text"]["content"]
    assert card.payload["card"]["header"]["title"]["content"] == "请确认 2 项操作"
    assert elements[0]["text"]["content"].startswith("1. ")
    assert '"SKU-1"' in text and '"SKU-2"' in text and "POST /api/stock/documents" in text
    assert card.target == {"chat_id": "oc_private"}

    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_done"
    assert posts(fake) == ["/api/stock/documents", "/api/stock/documents"]
    assert {d.get("sku") for d in fake.documents.values()} >= {"SKU-1", "SKU-2"}
    assert await outcomes(db_session) == [
        ("createDraft", "proposed"),
        ("createDraft", "proposed"),
        ("createDraft", "confirmed_ok"),
        ("createDraft", "confirmed_ok"),
    ]
    assert (await batch(db_session)).status == "submitted"
    assert "✅" in await last_update(db_session)
    (follow,) = await resumed(db_session)
    assert follow.inbound_event_id == task.inbound_event_id
    assert "成功" in follow.payload[confirm.RESULT_KEY]

    # The same card cannot run twice.
    again_click = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, again_click) == "expired"
    assert len(posts(fake)) == 2


async def test_idempotency_key_is_fixed_when_staged(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    (staged,) = await stage(
        client, app, task, user, {"system": "stock", "operation_id": "adjustStock"}
    )
    assert staged["status"] == "pending_confirmation"
    await release(app, task)
    state = await batch(db_session)
    (item,) = confirm.unseal(app.state.cipher, task.id, state.state)
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_done"
    assert fake.seen_keys == [item["key"]]  # type: ignore[attr-defined]


async def test_destructive_runs_after_confirmation_with_a_red_card(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app, "group")
    args = {
        "system": "stock",
        "operation_id": "cancelDocument",
        "path_params": {"id": "D1"},
        "body": {"reason": "录入错误"},
        "note": {"target": "单据 D1", "changes": [{"field": "status", "before": "confirmed"}]},
    }
    (staged,) = await stage(client, app, task, user, args)
    assert staged["status"] == "pending_confirmation", staged
    await release(app, task)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).one()
    assert card.payload["card"]["header"]["template"] == "red"
    elements = card.payload["card"]["body"]["elements"]
    summary = elements[0]["text"]["content"]
    detail = elements[1]["elements"][0]["text"]["content"]
    assert "无法恢复" in summary and "对象：单据 D1" in summary
    assert "status：原为 confirmed" in summary
    assert elements[1]["tag"] == "collapsible_panel" and elements[1]["expanded"] is False
    assert "/documents/" in detail and "录入错误" in detail and "/documents/" not in summary
    assert card.target["chat_id"] == "oc_group"
    state = await batch(db_session)
    pressed = await press(db_session, bot, task, state.id, chat_type="group")
    assert await run_click(db_session, db_engine, pressed) == "systems_write_done"
    assert posts(fake) == ["/api/stock/documents/D1/cancel"]


async def test_cancel_and_other_clickers_run_nothing(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app, "group")
    await stage(client, app, task, user, draft("SKU-1"))
    await release(app, task)
    state = await batch(db_session)
    stranger = await press(
        db_session, bot, task, state.id, chat_type="group", user_id="other", open_id="ou_other"
    )
    assert await run_click(db_session, db_engine, stranger) == "ignored"
    cancel = await press(db_session, bot, task, state.id, "cancel", chat_type="group")
    assert await run_click(db_session, db_engine, cancel) == "systems_write_cancelled"
    assert posts(fake) == [] and (await batch(db_session)).status == "cancelled"
    (follow,) = await resumed(db_session)
    assert "取消" in follow.payload[confirm.RESULT_KEY]


async def test_expired_card_runs_nothing(client, app, db_session, db_engine, fake) -> None:
    bot, user, task = await turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"))
    await release(app, task)
    state = await batch(db_session)
    state.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "expired"
    )
    assert posts(fake) == []


async def test_an_expired_card_is_resent_with_the_same_content_and_confirmed_again(
    client, app, db_session, db_engine, fake
) -> None:
    """过期的卡改成「已过期」并带「重新发起」：同一份暂存内容发一张新卡（新的 60 分钟），
    确认时照样整批复查；已确认的不能再重发。"""
    assert confirm.TTL == timedelta(minutes=60)
    bot, user, task = await turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"))
    await release(app, task)
    state = await batch(db_session)
    state.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    late = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, late) == "expired"
    assert "重新发起" in await last_update(db_session) and posts(fake) == []
    resend = await press(db_session, bot, task, state.id, confirm.RESEND)
    assert await run_click(db_session, db_engine, resend) == "systems_write_resent"
    first, again = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.kind == "send").order_by(OutboxItem.id)
        )
    )
    assert again.dedupe_key == f"systems-write:{state.id}:resend:1"
    assert again.payload["card"]["body"] == first.payload["card"]["body"]
    assert (await batch(db_session)).expires_at > utcnow() + timedelta(minutes=59)
    pressed = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, pressed) == "systems_write_done"
    assert posts(fake) == ["/api/stock/documents"]
    no_more = await press(db_session, bot, task, state.id, confirm.RESEND)
    assert await run_click(db_session, db_engine, no_more) == "ignored"


async def test_revoked_write_permission_voids_the_whole_batch(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"), draft("SKU-2"))
    await release(app, task)
    grant = await db_session.get(BotSystemGrant, (bot.id, "stock"))
    assert grant is not None
    grant.allow_write = False
    await db_session.commit()
    state = await batch(db_session)
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_refused" and posts(fake) == []
    assert "第 1 项" in await last_update(db_session)


async def test_changed_operation_voids_the_batch(client, app, db_session, db_engine, fake) -> None:
    bot, user, task = await turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"))
    await release(app, task)
    state = await batch(db_session)
    items = confirm.unseal(app.state.cipher, task.id, state.state)
    items[0]["op_digest"] = "something-else"
    state.state = {**state.state, "enc": confirm.seal(app.state.cipher, task.id, items)}
    await db_session.commit()
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_refused" and posts(fake) == []


async def test_first_failure_stops_the_rest(client, app, db_session, db_engine, fake) -> None:
    bot, user, task = await turn(db_session, app)
    fake.routes[("POST", "/api/stock/documents")] = lambda request: httpx.Response(
        500, json={"code": 500}
    )
    await stage(client, app, task, user, draft("SKU-1"), draft("SKU-2"))
    await release(app, task)
    state = await batch(db_session)
    result = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
    assert result == "systems_write_failed"
    assert posts(fake) == ["/api/stock/documents"]
    update = await last_update(db_session)
    assert "❌" in update and "没有成功" in update and "没有执行" in update
    (follow,) = await resumed(db_session)
    assert "未执行" in follow.payload[confirm.RESULT_KEY]


async def test_batch_is_capped(client, app, db_session, fake, monkeypatch) -> None:
    monkeypatch.setattr(confirm, "MAX_ITEMS", 2)
    _, user, task = await turn(db_session, app)
    *_, third = await stage(client, app, task, user, draft("A"), draft("B"), draft("C"))
    assert third["error"] == "confirmation_batch_full"
    assert (await batch(db_session)).state["count"] == 2


async def test_unfinished_turn_drops_its_batch(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    await stage(client, app, task, user, draft("SKU-1"))
    assert not await release(app, task, send=False)
    assert (await batch(db_session)).status == "cancelled"
    assert not await db_session.scalar(select(OutboxItem.id).where(OutboxItem.kind == "send"))


async def test_turns_nobody_can_confirm_stage_nothing(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app, "group")
    task.payload = {**task.payload, "origin": "observe"}
    await db_session.commit()
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["error"] == "confirmation_unavailable"
    assert not await db_session.scalar(select(InteractionState.id))


async def test_write_needs_the_grant_before_staging(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app, write=False)
    (out,) = await stage(client, app, task, user, draft("SKU-1"))
    assert out["error"] == "write_not_allowed"
    assert not await db_session.scalar(select(InteractionState.id))
