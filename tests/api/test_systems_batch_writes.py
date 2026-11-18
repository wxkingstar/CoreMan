"""Batch writes: one systems_call stages a whole file of items, one card confirms them, and the
platform runs them one by one in background tasks, carrying on after a failed item."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.db.models import (
    BotSystemGrant,
    BusinessSystem,
    InboundEvent,
    InteractionState,
    OutboxItem,
    SystemBatchItem,
    SystemCall,
    Task,
)
from coreman.core.systems_catalog import batch, confirm, proxy
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.systems_batch_cards import SystemsBatchRunHandler
from tests.api.test_systems_catalog import bearer, call
from tests.api.test_systems_proxy_upload import upload
from tests.api.test_systems_write_confirm import CHATS, turn
from tests.fakes.business_system import FakeBusinessSystem, install
from tests.integration.worker_helpers import build_ctx


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    sent: list[dict[str, Any]] = []

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append({**body, "key": request.headers.get("idempotency-key")})
        if body["sku"].startswith("BAD"):
            return httpx.Response(422, json={"code": 422, "message": "sku rejected"})
        return httpx.Response(200, json={"code": 0, "data": {"sku": body["sku"]}})

    system.routes[("POST", "/api/stock/documents")] = create
    system.sent = sent  # type: ignore[attr-defined]
    install(monkeypatch, system)
    return system


@pytest.fixture(autouse=True)
def storage(app: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"object_storage_root": str(tmp_path)}
    )
    monkeypatch.setattr(proxy, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(batch, "get_settings", lambda: app.state.settings)


def lines(*rows: dict[str, Any] | str) -> bytes:
    return "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows).encode()


def row(sku: str, quantity: int = 1, **extra: Any) -> dict[str, Any]:
    return {"body": {"sku": sku, "quantity": quantity}, **extra}


async def submit(
    client: Any, app: Any, task: Task, user: Any, data: bytes, **extra: Any
) -> dict[str, Any]:
    auth = bearer(app, task, user)
    uploaded = await upload(client, auth, data, "items.jsonl", "application/x-ndjson")
    assert uploaded.status_code == 200, uploaded.text
    args = {
        "system": "stock",
        "operation_id": "createDraft",
        "batch": {"upload_id": uploaded.json()["upload_id"]},
        **extra,
    }
    return await call(client, auth, "systems_call", args)


async def release(app: Any, task: Task, *, send: bool = True) -> bool:
    async with app.state.session_factory() as session:
        sent = await batch.release(
            session, app.state.cipher, bot_id=task.bot_id, task_id=task.id, send=send
        )
        await session.commit()
    return sent


async def state_of(db_session: AsyncSession) -> InteractionState:
    state = await db_session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == batch.KIND)
        .execution_options(populate_existing=True)
    )
    assert state is not None
    return state


async def click(
    session: AsyncSession, db_engine: Any, bot: Any, original: Task, state_id: uuid.UUID, verb: str
) -> str:
    card = await session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "send").order_by(OutboxItem.id.desc())
    )
    assert card is not None
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    chat_id = CHATS["single"]
    action = {
        "task_id": f"personal:{original.id}",
        "event_key": batch.CARD_EVENT,
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="single",
        chat_id=chat_id,
        sender_platform_user_id="human",
        sender_open_id="ou_human",
        reply_context={"chat_id": chat_id, "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-test",
                },
                "event": {
                    "operator": {"user_id": "human", "open_id": "ou_human"},
                    "context": {"open_chat_id": chat_id, "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(event)
    await session.flush()
    pressed = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": "human"},
            session_key=chat_id,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert pressed is not None
    pressed.status = "running"
    await session.commit()
    await CardActionHandler().run(build_ctx(db_engine, pressed))
    done = await session.get(Task, pressed.id, populate_existing=True)
    assert done is not None and done.result is not None
    return str(done.result["card"])


async def run_next(db_session: AsyncSession, db_engine: Any) -> bool:
    """Run the oldest queued run task; False when there is none."""
    task = await db_session.scalar(
        select(Task)
        .where(Task.kind == batch.RUN_KIND, Task.status == "queued")
        .order_by(Task.id)
        .execution_options(populate_existing=True)
    )
    if task is None:
        return False
    task.status = "running"
    await db_session.commit()
    await SystemsBatchRunHandler().run(build_ctx(db_engine, task))
    return True


async def run_all(db_session: AsyncSession, db_engine: Any) -> int:
    count = 0
    while await run_next(db_session, db_engine):
        count += 1
    return count


async def statuses(db_session: AsyncSession) -> list[str]:
    rows = await db_session.scalars(
        select(SystemBatchItem.status)
        .order_by(SystemBatchItem.seq)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def updates(db_session: AsyncSession) -> list[dict[str, Any]]:
    rows = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.kind == "card_update").order_by(OutboxItem.id)
    )
    return [item.payload["card"] for item in rows]


def text_of(card: dict[str, Any]) -> str:
    return str(card["body"]["elements"][0]["text"]["content"])


async def resumed(db_session: AsyncSession) -> list[Task]:
    rows = await db_session.scalars(select(Task).where(Task.dedupe_key.like("systems-write:%")))
    return list(rows)


async def test_a_batch_is_confirmed_once_and_runs_past_a_failed_item(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    data = lines(
        row("SKU-1", target="商品 SKU-1", before={"quantity": 9}),
        row("BAD-2", 2),
        row("SKU-3", 3),
    )
    out = await submit(client, app, task, user, data, note={"impact": "新建草稿"})
    assert out["status"] == "pending_confirmation" and out["count"] == 3
    assert fake.sent == []  # type: ignore[attr-defined]
    state = await state_of(db_session)
    # Staged items are sealed; only who, where and how many are readable.
    assert "SKU-1" not in str(state.state) and state.state["count"] == 3
    items = list(await db_session.scalars(select(SystemBatchItem)))
    assert len(items) == 3 and all("SKU" not in i.request_enc for i in items)

    assert await release(app, task)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).one()
    body = card.payload["card"]
    assert body["header"]["title"]["content"] == "请确认 3 项操作"
    text = text_of(body)
    assert "共 3 项" in text and "【字段统计】" in text and "quantity：" in text
    assert "1. 商品 SKU-1  sku：SKU-1；quantity：9 → 1" in text
    assert body["body"]["elements"][1]["content"].startswith("[下载全部明细（CSV）](")

    result = await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert result == "systems_batch_started"
    assert (await state_of(db_session)).status == "submitted"
    assert "已处理 0 / 3" in text_of((await updates(db_session))[-1])
    assert fake.sent == []  # type: ignore[attr-defined]

    assert await run_all(db_session, db_engine) == 1
    assert [s["sku"] for s in fake.sent] == ["SKU-1", "BAD-2", "SKU-3"]  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok", "http_error", "ok"]
    final = (await updates(db_session))[-1]
    assert final["header"]["title"]["content"] == "没有全部完成"
    assert "成功 2，失败 1" in text_of(final) and "第 2 项" in text_of(final)
    assert final["body"]["elements"][1]["content"].startswith("[下载全部结果（CSV）](")
    (follow,) = await resumed(db_session)
    note = follow.payload[confirm.RESULT_KEY]
    assert "成功 2，失败 1" in note and "sku rejected" in note
    recorded = await db_session.scalars(select(SystemCall.outcome).order_by(SystemCall.id))
    assert list(recorded) == [
        "batch_proposed",
        "confirmed_ok",
        "confirmed_http_error",
        "confirmed_ok",
    ]
    # The card cannot run twice.
    assert await click(db_session, db_engine, bot, task, state.id, "confirm") == "expired"
    assert len(fake.sent) == 3  # type: ignore[attr-defined]


async def test_any_bad_line_stages_nothing(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    data = lines(row("SKU-1"), "{not json", {"body": {"sku": "X"}}, row("SKU-1"), "")
    out = await submit(client, app, task, user, data)
    assert out["error"] == "invalid_batch" and out["problem_count"] == 3
    joined = "\n".join(out["problems"])
    assert "第 2 行" in joined and "第 3 行" in joined and "与第 1 行完全相同" in joined
    assert not await db_session.scalar(select(InteractionState.id))


async def test_the_cap_is_the_systems_setting_for_the_risk(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    await db_session.execute(
        update(BusinessSystem).where(BusinessSystem.key == "stock").values(batch_write_max=2)
    )
    await db_session.commit()
    auth = bearer(app, task, user)
    described = await call(
        client, auth, "systems_describe", {"system": "stock", "operation_id": "createDraft"}
    )
    assert described["call"]["batch"]["max_items"] == 2
    out = await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    assert out["error"] == "invalid_batch" and "最多 2 项" in out["problems"][0]
    out = await submit(client, app, task, user, lines(row("A"), row("B")))
    assert out["count"] == 2


async def test_proxied_calls_are_counted_per_system_without_fingerprints(
    client, app, db_session, fake
) -> None:
    _, user, task = await turn(db_session, app)
    await db_session.execute(
        update(BusinessSystem).where(BusinessSystem.key == "stock").values(proxy_calls_per_turn=2)
    )
    await db_session.commit()
    auth = bearer(app, task, user)
    read = {"system": "stock", "operation_id": "listDocuments", "query": {"status": "confirmed"}}
    for _ in range(2):
        assert "error" not in await call(client, auth, "systems_call", read)
    over = await call(client, auth, "systems_call", read)
    assert over["error"] == "catalog_budget_exhausted" and "2 次" in over["message"]
    await db_session.refresh(task)
    state = task.payload["systems_catalog"]
    assert state["proxy_calls_by_system"] == {"stock": 3} and not state.get("seen")


async def test_items_wait_the_systems_interval(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(batch, "_sleep", record)
    bot, user, task = await turn(db_session, app)
    await db_session.execute(
        update(BusinessSystem).where(BusinessSystem.key == "stock").values(batch_interval_ms=250)
    )
    await db_session.commit()
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "ok", "ok"]
    # 第一项不等，之后每项发出前等一个间隔。
    assert waits == [0.25, 0.25]


async def test_an_interval_longer_than_the_stretch_queues_the_next_one_later(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "_sleep", pytest.fail)
    bot, user, task = await turn(db_session, app)
    await db_session.execute(
        update(BusinessSystem).where(BusinessSystem.key == "stock").values(batch_interval_ms=60000)
    )
    await db_session.commit()
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert await run_next(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "pending"]
    queued = await db_session.scalar(
        select(Task)
        .where(Task.kind == batch.RUN_KIND, Task.status == "queued")
        .execution_options(populate_existing=True)
    )
    assert queued is not None
    assert queued.run_after > utcnow() + timedelta(seconds=50)
    assert await run_next(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "ok"]


async def test_one_batch_per_turn_and_writes_only(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    assert (await submit(client, app, task, user, lines(row("A"))))["count"] == 1
    again = await submit(client, app, task, user, lines(row("B")))
    assert again["error"] == "batch_exists"
    auth = bearer(app, task, user)
    uploaded = await upload(client, auth, lines(row("A")), "r.jsonl", "application/x-ndjson")
    read = await call(
        client,
        auth,
        "systems_call",
        {
            "system": "stock",
            "operation_id": "listDocuments",
            "batch": {"upload_id": uploaded.json()["upload_id"]},
        },
    )
    assert read["error"] == "batch_read_only"


async def test_turns_nobody_can_confirm_stage_nothing(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app, "group")
    task.payload = {**task.payload, "origin": "observe"}
    await db_session.commit()
    out = await submit(client, app, task, user, lines(row("A")))
    assert out["error"] == "confirmation_unavailable"
    assert not await db_session.scalar(select(InteractionState.id))


async def test_a_quick_small_batch_does_not_refresh_progress_midway(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    # 默认节奏下，几秒内跑完的小批量不在中途刷新进度卡，直接出结果。
    assert await run_all(db_session, db_engine) == 1
    texts = [text_of(card) for card in await updates(db_session)]
    assert not any("已处理 1 / 3" in text or "已处理 2 / 3" in text for text in texts)


async def test_progress_shows_within_a_stretch_when_due(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "PROGRESS_MIN_SECONDS", 0)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("D"), row("E"), row("F")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    before = len(await updates(db_session))
    # 不分段也会刷新：每推进 1%（至少 1 项）就到期。
    assert await run_all(db_session, db_engine) == 1
    texts = [text_of(card) for card in (await updates(db_session))[before:]]
    assert any("已处理 1 / 3" in text for text in texts)
    assert any("已处理 2 / 3" in text for text in texts)


def test_progress_is_due_by_time_and_by_one_percent() -> None:
    start = utcnow()
    meta = {"count": 50000, "progress_at": start.isoformat(), "progress_done": 1000}
    at = lambda seconds: start + timedelta(seconds=seconds)  # noqa: E731
    assert not batch.progress_due(meta, 5000, at(9))
    assert not batch.progress_due(meta, 1499, at(30))
    assert batch.progress_due(meta, 1500, at(30))
    assert batch.progress_due(meta, 1001, at(60))
    small = {"count": 3, "progress_at": start.isoformat(), "progress_done": 0}
    assert batch.progress_due(small, 1, at(10)) and not batch.progress_due(small, 0, at(30))
    assert batch.progress_due({"count": 3}, 0, at(0))


async def test_runs_in_chunks_and_stops_when_asked(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    monkeypatch.setattr(batch, "PROGRESS_MIN_SECONDS", 0)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert await run_next(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "pending", "pending"]
    assert "已处理 1 / 3" in text_of((await updates(db_session))[-1])

    assert await click(db_session, db_engine, bot, task, state.id, batch.STOP) == (
        "systems_batch_stopping"
    )
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped", "skipped"]
    assert len(fake.sent) == 1  # type: ignore[attr-defined]
    assert "按发起人的要求停止" in text_of((await updates(db_session))[-1])


async def test_an_item_left_sending_is_unknown_and_never_sent_again(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    # 模拟上一段发出第 1 项后进程没了。
    await db_session.execute(
        update(SystemBatchItem).where(SystemBatchItem.seq == 1).values(status="sending")
    )
    await db_session.commit()
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["unknown", "ok"]
    assert [s["sku"] for s in fake.sent] == ["B"]  # type: ignore[attr-defined]


async def test_revoked_write_permission_halts_the_rest(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert await run_next(db_session, db_engine)
    await db_session.execute(update(BotSystemGrant).values(allow_write=False))
    await db_session.commit()
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped", "skipped"]
    assert "中途停下" in text_of((await updates(db_session))[-1])
    (follow,) = await resumed(db_session)
    assert "中途停下" in follow.payload[confirm.RESULT_KEY]


async def test_a_lost_run_task_is_revived(client, app, db_session, db_engine, fake) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    await db_session.execute(
        update(Task).where(Task.kind == batch.RUN_KIND).values(status="failed")
    )
    await db_session.commit()
    async with app.state.session_factory() as session:
        assert await batch.revive(session, batch.utcnow()) == 1
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok"]


async def test_cancel_runs_nothing(client, app, db_session, db_engine, fake) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A")))
    await release(app, task)
    state = await state_of(db_session)
    assert await click(db_session, db_engine, bot, task, state.id, "cancel") == (
        "systems_batch_cancelled"
    )
    assert await run_all(db_session, db_engine) == 0
    assert fake.sent == []  # type: ignore[attr-defined]


async def test_cancel_after_confirm_cannot_undo_or_pretend(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    """The confirm click re-checks the operation, which commits and lets go of the row lock: a
    cancel landing there must find the batch already confirmed, not cancel it on the card while
    it runs anyway."""
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    checked = batch.checked
    late: list[str] = []

    async def racing(pctx: Any, staged: Any) -> Any:
        out = await checked(pctx, staged)
        if not late:
            late.append("")
            late[0] = await click(db_session, db_engine, bot, task, state.id, "cancel")
        return out

    monkeypatch.setattr(batch, "checked", racing)
    assert await click(db_session, db_engine, bot, task, state.id, "confirm") == (
        "systems_batch_started"
    )
    assert late == ["expired"]
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "ok"]
    texts = [text_of(card) for card in await updates(db_session)]
    assert not any("已取消" in text for text in texts)
    (follow,) = await resumed(db_session)
    assert "成功 2" in follow.payload[confirm.RESULT_KEY]


async def test_a_run_task_taken_for_lost_steps_aside(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    """Its run task was judged lost and another stretch queued in its place, but the first one is
    still alive: it stops at the next item and neither queues a stretch nor finishes the batch."""
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    send = proxy.send
    calls = 0

    async def judged_lost_meanwhile(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        out = await send(*args, **kwargs)
        if calls == 1:
            async with app.state.session_factory() as session:
                await session.execute(
                    update(Task).where(Task.kind == batch.RUN_KIND).values(status="failed")
                )
                await session.commit()
                assert await batch.revive(session, batch.utcnow()) == 1
        return out

    monkeypatch.setattr(proxy, "send", judged_lost_meanwhile)
    first = await db_session.scalar(select(Task).where(Task.kind == batch.RUN_KIND))
    assert first is not None
    await SystemsBatchRunHandler().run(build_ctx(db_engine, first))
    assert [s["sku"] for s in fake.sent] == ["A"]  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok", "pending", "pending"]
    current = await state_of(db_session)
    assert current.state["chunk"] == 2 and "finished_at" not in current.state
    assert await resumed(db_session) == []

    # The stretch queued in its place carries on, in order, each item once.
    assert await run_all(db_session, db_engine) == 1
    assert [s["sku"] for s in fake.sent] == ["A", "B", "C"]  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok", "ok", "ok"]
    # A late start of the superseded task does nothing either.
    first.status = "running"
    await db_session.commit()
    await SystemsBatchRunHandler().run(build_ctx(db_engine, first))
    assert len(fake.sent) == 3  # type: ignore[attr-defined]
    assert len(await resumed(db_session)) == 1


async def test_an_item_possibly_in_flight_is_waited_for(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    # 上一段刚把第 1 项标成正在发送，它的进程可能还活着。
    await db_session.execute(
        update(SystemBatchItem)
        .where(SystemBatchItem.seq == 1)
        .values(status="sending", started_at=batch.utcnow())
    )
    await db_session.commit()
    assert await run_next(db_session, db_engine)
    assert await statuses(db_session) == ["sending", "pending"]
    assert fake.sent == []  # type: ignore[attr-defined]
    waiting = await db_session.scalar(
        select(Task).where(Task.kind == batch.RUN_KIND).execution_options(populate_existing=True)
    )
    assert waiting is not None and waiting.status == "queued"
    assert waiting.run_after > batch.utcnow() + timedelta(seconds=batch.SEND_GRACE - 30)

    # 过了宽限仍没有结果：结果未知，不重发，接着做后面的。
    await db_session.execute(
        update(SystemBatchItem)
        .where(SystemBatchItem.seq == 1)
        .values(started_at=batch.utcnow() - timedelta(seconds=batch.SEND_GRACE + 1))
    )
    await db_session.commit()
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["unknown", "ok"]
    assert [s["sku"] for s in fake.sent] == ["B"]  # type: ignore[attr-defined]


async def test_finish_never_calls_a_sending_item_not_run(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    await db_session.execute(
        update(SystemBatchItem).where(SystemBatchItem.seq == 1).values(status="sending")
    )
    await db_session.commit()
    async with app.state.session_factory() as session:
        current = await session.get(InteractionState, state.id)
        assert current is not None
        note = await batch.finish(session, app.state.cipher, current, "停下", chunk=1)
        await session.commit()
    assert await statuses(db_session) == ["unknown", "skipped"]
    assert note is not None and "结果未知 1" in note and "不要直接重做" in note
    # A task that is no longer the current one finishes nothing.
    async with app.state.session_factory() as session:
        current = await session.get(InteractionState, state.id)
        assert current is not None
        assert await batch.finish(session, app.state.cipher, current, None, chunk=1) is None


async def test_a_timeout_is_unknown_and_a_refused_connection_is_a_failure(
    client, app, db_session, db_engine, fake
) -> None:
    def create(request: httpx.Request) -> httpx.Response:
        sku = json.loads(request.content)["sku"]
        fake.sent.append({"sku": sku})  # type: ignore[attr-defined]
        if sku == "SLOW":
            raise httpx.ReadTimeout("no response", request=request)
        if sku == "DOWN":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"code": 0})

    fake.routes[("POST", "/api/stock/documents")] = create
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("SLOW"), row("DOWN"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["unknown", "unreachable", "ok"]
    final = text_of((await updates(db_session))[-1])
    assert "成功 1，失败 1，结果未知 1" in final and "不要直接重做" in final
    (follow,) = await resumed(db_session)
    note = follow.payload[confirm.RESULT_KEY]
    assert "可能已经处理" in note and "先用读操作逐项核对" in note


async def test_a_batch_that_keeps_losing_its_run_task_is_finished_not_left_running(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    now = batch.utcnow()
    outcomes = []
    for attempt in range(batch.MAX_REVIVES + 2):
        await db_session.execute(
            update(Task).where(Task.kind == batch.RUN_KIND).values(status="failed")
        )
        await db_session.commit()
        async with app.state.session_factory() as session:
            when = now + timedelta(seconds=(attempt + 1) * (batch.REVIVE_GAP + 1))
            outcomes.append(await batch.revive(session, when))
        # 刚补排的那一段又丢了：太快再补一次不算数。
        await db_session.execute(
            update(Task).where(Task.kind == batch.RUN_KIND).values(status="failed")
        )
        await db_session.commit()
        async with app.state.session_factory() as session:
            assert await batch.revive(session, when + timedelta(seconds=1)) == 0
    assert outcomes == [1, 1, 1, 1, 0]
    final = await state_of(db_session)
    assert final.state.get("finished_at") and final.expires_at is not None
    assert await statuses(db_session) == ["skipped", "skipped"]
    assert "多次意外中断" in text_of((await updates(db_session))[-1])
    (follow,) = await resumed(db_session)
    assert "多次意外中断" in follow.payload[confirm.RESULT_KEY]
    assert fake.sent == []  # type: ignore[attr-defined]


async def test_progress_between_losses_does_not_count_towards_giving_up(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(*(row(f"S{i}") for i in range(6))))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    now = batch.utcnow()
    for attempt in range(batch.MAX_REVIVES + 2):
        # 每次都先做完一项，再丢掉排好的下一段。
        assert await run_next(db_session, db_engine)
        await db_session.execute(
            update(Task)
            .where(Task.kind == batch.RUN_KIND, Task.status == "queued")
            .values(status="failed")
        )
        await db_session.commit()
        async with app.state.session_factory() as session:
            when = now + timedelta(seconds=(attempt + 1) * (batch.REVIVE_GAP + 1))
            assert await batch.revive(session, when) == 1
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok"] * 6
    assert len(fake.sent) == 6  # type: ignore[attr-defined]


async def test_an_item_that_makes_sending_raise_is_unknown_and_the_rest_runs(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("BOOM"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    send = proxy.send

    async def crashing(pctx: Any, prepared: Any, **kwargs: Any) -> Any:
        if prepared.body["sku"] == "BOOM":
            raise ValueError("cannot serialise")
        return await send(pctx, prepared, **kwargs)

    monkeypatch.setattr(proxy, "send", crashing)
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "unknown", "ok"]
    assert [s["sku"] for s in fake.sent] == ["A", "C"]  # type: ignore[attr-defined]
    final = text_of((await updates(db_session))[-1])
    assert "成功 2，失败 0，结果未知 1" in final and "ValueError" in final


async def test_nan_is_refused_when_the_batch_is_submitted(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    out = await submit(
        client, app, task, user, lines(row("A"), '{"body": {"sku": "B", "quantity": NaN}}')
    )
    assert out["error"] == "invalid_batch" and "第 2 行" in out["problems"][0]


async def test_a_batch_too_wide_for_a_card_is_refused_at_once(
    client, app, db_session, fake, monkeypatch
) -> None:
    monkeypatch.setattr(confirm, "CARD_BYTES_MAX", 1500)
    _, user, task = await turn(db_session, app)
    out = await submit(client, app, task, user, lines(row("A"), row("B")))
    assert out["error"] == "batch_card_too_large"
    assert not await db_session.scalar(select(InteractionState.id))


async def test_no_detail_file_no_card(client, app, db_session, fake, monkeypatch) -> None:
    async def unavailable(*args: Any, **kwargs: Any) -> None:
        return None

    _, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    monkeypatch.setattr(batch, "_csv_link", unavailable)
    assert not await release(app, task)
    assert (await state_of(db_session)).status == "cancelled"
    (notice,) = await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))
    text = notice.payload["card"]["body"]["elements"][0]["text"]["content"]
    assert "2 项写操作的明细文件没能生成" in text and "不会执行" in text


async def test_a_deactivated_asker_or_a_moved_server_halts_the_rest(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B"), row("C")))
    await release(app, task)
    state = await state_of(db_session)
    assert state.state["server"]
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert await run_next(db_session, db_engine)
    monkeypatch.setattr(batch, "_server", lambda system, loaded: "https://elsewhere.example.com")
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped", "skipped"]
    assert "接口地址已经变化" in text_of((await updates(db_session))[-1])


async def test_a_disabled_employee_halts_the_rest(
    client, app, db_session, db_engine, fake, monkeypatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_ITEMS", 1)
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    assert await run_next(db_session, db_engine)
    bot.enabled = False
    await db_session.commit()
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok", "skipped"]
    assert "AI 员工已停用" in text_of((await updates(db_session))[-1])


async def test_cancelling_the_run_task_stops_the_batch_and_a_lost_one_steps_aside(
    client, app, db_session, db_engine, fake
) -> None:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A"), row("B")))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    run = await db_session.scalar(select(Task).where(Task.kind == batch.RUN_KIND))
    assert run is not None
    run.status = "running"
    await db_session.commit()
    # 被判失联：什么都不做，也不收尾，等巡检另排一段。
    lost = build_ctx(db_engine, run)
    lost.request_cancel("reaper")
    await SystemsBatchRunHandler().run(lost)
    assert await statuses(db_session) == ["pending", "pending"]
    assert "finished_at" not in (await state_of(db_session)).state
    # 管理台停止：这一批到此为止。
    run.status = "running"
    await db_session.commit()
    stopped = build_ctx(db_engine, run)
    stopped.request_cancel("admin")
    await SystemsBatchRunHandler().run(stopped)
    assert await statuses(db_session) == ["skipped", "skipped"]
    assert "执行被管理员停止" in text_of((await updates(db_session))[-1])
    assert fake.sent == []  # type: ignore[attr-defined]


async def confirmed(
    client: Any, app: Any, db_session: AsyncSession, db_engine: Any, *skus: str
) -> tuple[Any, Task, InteractionState]:
    bot, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(*(row(sku) for sku in skus)))
    await release(app, task)
    state = await state_of(db_session)
    await click(db_session, db_engine, bot, task, state.id, "confirm")
    return bot, task, state


async def notices(db_session: AsyncSession) -> list[str]:
    rows = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.dedupe_key.like("%:notice:%")).order_by(OutboxItem.id)
    )
    return [item.payload["card"]["body"]["elements"][0]["content"] for item in rows]


def buttons(card: dict[str, Any]) -> dict[str, str]:
    """Button label → verb, wherever the buttons sit on the card."""
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                verb = node["behaviors"][0]["value"]["level"].split(":")[0]
                found[node["text"]["content"]] = verb
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return found


async def test_a_large_batch_runs_a_few_items_then_waits_for_the_asker(
    client, app, db_session, db_engine, fake
) -> None:
    skus = [f"S{i}" for i in range(12)]
    bot, task, state = await confirmed(client, app, db_session, db_engine, *skus)
    card = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).first()
    assert "先执行前 5 项并暂停" in text_of(card.payload["card"])

    assert await run_all(db_session, db_engine) == 1
    assert [s["sku"] for s in fake.sent] == skus[:5]  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok"] * 5 + ["pending"] * 7
    paused = (await updates(db_session))[-1]
    assert paused["header"]["title"]["content"] == "已试跑 5 项，等你确认"
    assert "第 5 项" in text_of(paused) and "核对" in text_of(paused)
    assert buttons(paused) == {"继续执行剩余 7 项": batch.GO, "停止，剩余的不执行": batch.STOP}
    assert "试跑前 5 项并暂停" in (await notices(db_session))[-1]
    # Paused: nothing is queued, and the patrol does not queue anything either.
    async with app.state.session_factory() as session:
        assert await batch.revive(session, batch.utcnow()) == 0
    assert await run_all(db_session, db_engine) == 0

    assert await click(db_session, db_engine, bot, task, state.id, batch.GO) == (
        "systems_batch_resumed"
    )
    await run_all(db_session, db_engine)
    assert [s["sku"] for s in fake.sent] == skus  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok"] * 12
    assert (await updates(db_session))[-1]["header"]["title"]["content"] == "已完成"
    # No second trial, and a second "carry on" does nothing.
    assert await click(db_session, db_engine, bot, task, state.id, batch.GO) == "ignored"


async def test_stopping_a_paused_batch_ends_it_and_an_unanswered_pause_ends_too(
    client, app, db_session, db_engine, fake
) -> None:
    skus = [f"S{i}" for i in range(12)]
    bot, task, state = await confirmed(client, app, db_session, db_engine, *skus)
    await run_all(db_session, db_engine)
    assert await click(db_session, db_engine, bot, task, state.id, batch.STOP) == (
        "systems_batch_stopped"
    )
    assert await statuses(db_session) == ["ok"] * 5 + ["skipped"] * 7
    final = (await updates(db_session))[-1]
    assert "按发起人的要求停止" in text_of(final)
    assert buttons(final) == {"继续执行剩余 7 项": batch.CONTINUE}
    (follow,) = await resumed(db_session)
    assert "未执行 7" in follow.payload[confirm.RESULT_KEY]

    # 结束后再继续：只做没执行的，做完再续接一轮。
    assert await click(db_session, db_engine, bot, task, state.id, batch.CONTINUE) == (
        "systems_batch_resumed"
    )
    await run_all(db_session, db_engine)
    assert [s["sku"] for s in fake.sent] == skus  # type: ignore[attr-defined]
    assert await statuses(db_session) == ["ok"] * 12
    assert len(await resumed(db_session)) == 2
    assert await click(db_session, db_engine, bot, task, state.id, batch.CONTINUE) == "ignored"


async def test_a_pause_nobody_answers_ends_the_batch(
    client, app, db_session, db_engine, fake
) -> None:
    await confirmed(client, app, db_session, db_engine, *(f"S{i}" for i in range(12)))
    await run_all(db_session, db_engine)
    async with app.state.session_factory() as session:
        later = batch.utcnow() + batch.PAUSE_TTL + timedelta(seconds=1)
        assert await batch.revive(session, later) == 1
    assert await statuses(db_session) == ["ok"] * 5 + ["skipped"] * 7
    assert "60 分钟没有选择继续" in text_of((await updates(db_session))[-1])
    assert len(await resumed(db_session)) == 1


async def test_ten_failures_in_a_row_pause_the_batch_and_failed_items_can_be_retried(
    client, app, db_session, db_engine, fake
) -> None:
    skus = [f"S{i}" for i in range(5)] + [f"BAD{i}" for i in range(10)] + ["T1", "T2"]
    bot, task, state = await confirmed(client, app, db_session, db_engine, *skus)
    await run_all(db_session, db_engine)
    await click(db_session, db_engine, bot, task, state.id, batch.GO)
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok"] * 5 + ["http_error"] * 10 + ["pending"] * 2
    paused = (await updates(db_session))[-1]
    assert paused["header"]["title"]["content"] == "连续 10 项没有成功，已暂停"
    assert "sku rejected" in text_of(paused)
    assert "连续 10 项没有成功" in (await notices(db_session))[-1]

    await click(db_session, db_engine, bot, task, state.id, batch.GO)
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok"] * 5 + ["http_error"] * 10 + ["ok"] * 2
    final = (await updates(db_session))[-1]
    assert buttons(final) == {"重试失败的 10 项": batch.RETRY}

    # 业务系统恢复后重试：只重发失败的 10 项，用新的幂等键。
    before = {i.seq: i.idempotency_key for i in await db_session.scalars(select(SystemBatchItem))}
    fake.routes[("POST", "/api/stock/documents")] = lambda request: httpx.Response(
        200, json={"code": 0}
    )
    sent_before = len(fake.sent)  # type: ignore[attr-defined]
    assert await click(db_session, db_engine, bot, task, state.id, batch.RETRY) == (
        "systems_batch_resumed"
    )
    await run_all(db_session, db_engine)
    assert await statuses(db_session) == ["ok"] * 17
    assert len(fake.sent) == sent_before  # type: ignore[attr-defined]  # 新路由不记到 sent
    after = {
        i.seq: i.idempotency_key
        for i in await db_session.scalars(
            select(SystemBatchItem).execution_options(populate_existing=True)
        )
    }
    assert all((after[seq] != before[seq]) == (6 <= seq <= 15) for seq in before)
    assert (await updates(db_session))[-1]["header"]["title"]["content"] == "已完成"
    assert len(await resumed(db_session)) == 2


async def test_a_group_asker_is_mentioned(client, app, db_session, fake) -> None:
    _, user, task = await turn(db_session, app)
    await submit(client, app, task, user, lines(row("A")))
    state = await state_of(db_session)
    assert state.state["open_id"] == "ou_human"
    state.state = {**state.state, "chat_type": "group"}
    await batch.notify(db_session, state, "t", "批量操作已结束")
    await db_session.commit()
    assert (await notices(db_session))[-1] == "<at id=ou_human></at> 批量操作已结束"
