"""个人助理一轮里的飞书写：在场的发一张确认卡，无人在场的合成一张审批单；确认卡过期可重发。

正向：暂存不执行；卡片按顺序完整列出、不可见字符转义；所有者确认后按暂存的那一份原样、按顺序
执行一次；合单批准后按顺序执行、只投回一轮。反向：别人点、取消、过期、并发与重复点击、会话变了、
执行失败、没正常结束的一轮、同一任务重跑都不多执行；普通 AI 员工与不能当场点卡的轮次不走这里。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import digest, sealed, sent
from coreman.core.assistants.approvals import batch, execute, service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from coreman.core.systems_catalog import confirm as systems_confirm
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.approval_helpers import (
    CFG,
    OWNER,
    PEER,
    TEXT,
    FakeFeishu,
    FakeTower,
    assistant_turn,
    install,
    message,
    queued,
    reply,
)
from tests.integration.worker_helpers import MASTER, build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")

APP = "cli_example"
TENANT = "tenant-example"
CHAT = "oc_assistant"
SECOND = "周四下午三点前给你最终版。"


@pytest.fixture(autouse=True)
def tower_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """部署配好了审批系统：本轮结束时合并提交读的是部署配置。"""
    monkeypatch.setattr(tower, "config", lambda settings, target=tower.TARGET: CFG)


def second_reply(**approval: Any) -> dict[str, Any]:
    """同一轮里的第二个写：在同一个会话里再回一句（换正文与重试编号）。"""
    return {**reply(**approval), "text": SECOND, "uuid": "u2"}


async def present(session: AsyncSession) -> tuple[Bot, User, Scope]:
    """所有者本人在与助理的私聊里打字发来的一轮：入站事件带着可核验的原始事件。"""
    bot, owner, scope = await assistant_turn(session)
    event = await session.get(InboundEvent, scope.task.inbound_event_id)
    assert event is not None
    event.sender_open_id = OWNER
    event.payload = {
        **event.payload,
        "sender": {**event.payload["sender"], "sender_type": "user"},
        "raw": {
            "header": {"tenant_key": TENANT, "app_id": APP},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"user_id": "zhangsan", "open_id": OWNER},
                },
                "message": {"chat_type": "p2p", "chat_id": CHAT},
            },
        },
    }
    task = await session.get(Task, scope.task.id)
    assert task is not None
    task.user_id = owner.id
    await session.commit()
    return bot, owner, replace(scope, task=task, event=event)


async def stage(
    session: AsyncSession, scope: Scope, *calls: dict[str, Any]
) -> list[dict[str, Any]]:
    outs = []
    for arguments in calls:
        out = await batch.propose(
            session,
            None,  # type: ignore[arg-type]
            scope,
            "feishu_reply_message",
            arguments,
            cfg=CFG,
            files=None,
        )
        assert out is not None
        await session.commit()
        outs.append(out)
    return outs


async def state_of(session: AsyncSession) -> InteractionState:
    found = await session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == batch.KIND)
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def release(engine: AsyncEngine, scope: Scope, *, send: bool = True) -> str | None:
    async with make_session_factory(engine)() as session:
        out = await batch.release(session, bot_id=scope.bot.id, task_id=scope.task.id, send=send)
        await session.commit()
    return out


async def cards(session: AsyncSession) -> list[OutboxItem]:
    found = list(
        await session.scalars(
            select(OutboxItem)
            .where(OutboxItem.kind == "send")
            .order_by(OutboxItem.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, found)
    return found


def body_text(card: dict[str, Any]) -> str:
    return str(card["body"]["elements"][0]["text"]["content"])


async def last_update(session: AsyncSession) -> dict[str, Any]:
    item = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.kind == "card_update")
        .order_by(OutboxItem.id.desc())
        .execution_options(populate_existing=True)
    )
    assert item is not None
    return dict(item.payload["card"])


async def press(
    session: AsyncSession,
    bot: Bot,
    origin: Task,
    state_id: uuid.UUID,
    verb: str = "confirm",
    *,
    user_id: str = "zhangsan",
    open_id: str = OWNER,
) -> Task:
    """在最近发出的那张确认卡上点一下：回调按网关的样子落库并排成任务。"""
    card = (await cards(session))[-1]
    message_id = f"om_card_{card.id}"
    if card.status != "sent":
        card.status = "sent"
        card.payload = {**card.payload, "_feishu_message_id": message_id}
    message_id = str(card.payload["_feishu_message_id"])
    action = {
        "task_id": f"personal:{origin.id}",
        "event_key": batch.CARD_EVENT,
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="single",
        chat_id=CHAT,
        sender_platform_user_id=user_id,
        sender_open_id=open_id,
        reply_context={"chat_id": CHAT, "message_id": message_id},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": APP,
                    "tenant_key": TENANT,
                },
                "event": {
                    "operator": {"user_id": user_id, "open_id": open_id},
                    "context": {"open_chat_id": CHAT, "open_message_id": message_id},
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
            session_key=CHAT,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await session.commit()
    return task


async def click(session: AsyncSession, engine: AsyncEngine, pressed: Task) -> str:
    await CardActionHandler().run(build_ctx(engine, pressed))
    done = await session.get(Task, pressed.id, populate_existing=True)
    assert done is not None and done.result is not None
    return str(done.result["card"])


async def resumed(session: AsyncSession, state: InteractionState) -> str | None:
    found = await session.scalar(
        select(Task)
        .where(Task.dedupe_key == f"assistant-write:{state.id}:resume")
        .execution_options(populate_existing=True)
    )
    if found is None:
        return None
    await sealed.open_row(session, found)
    assert found.kind == "chat" and found.payload["serialize_session"] is True
    return str(found.payload[batch.RESULT_KEY])


async def staged_and_sent(
    session: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> tuple[Bot, Scope, InteractionState, FakeFeishu, FakeTower]:
    feishu, fake = install(monkeypatch)
    bot, _, scope = await present(session)
    await stage(session, scope, reply(), second_reply())
    assert await release(engine, scope) == batch.CARD
    return bot, scope, await state_of(session), feishu, fake


# 在场：确认卡 --------------------------------------------------------------------------------


async def test_present_writes_wait_for_one_card_that_lists_them_in_order(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    _, _, scope = await present(db_session)
    first, second = await stage(db_session, scope, reply(), second_reply())
    assert first["status"] == second["status"] == "pending_confirmation"
    assert second["pending"] == 2 and "确认卡" in second["hint"] and "不要重试" in second["hint"]
    # 同一份内容再调一次：不重复暂存。
    (again,) = await stage(db_session, scope, reply())
    assert again["duplicate"] is True and again["pending"] == 2
    # 不提交审批系统、不建审批行、不调飞书的写接口。
    assert fake.submitted == [] and feishu.writes == []
    assert await db_session.scalar(select(AssistantApproval)) is None
    state = await state_of(db_session)
    assert state.state["mode"] == batch.CARD and not state.state.get("sent")
    # 暂存的内容在库里是所有者数据密钥的密文。
    raw = await db_session.scalar(
        text("SELECT state::text FROM interaction_states WHERE id = :id"), {"id": state.id}
    )
    assert "_sealed" in raw and "周四" not in raw and "feishu_reply_message" not in raw
    assert await release(db_engine, scope) == batch.CARD
    assert await release(db_engine, scope) is None  # 只发一次
    (card,) = await cards(db_session)
    assert card.target == {"chat_id": CHAT}
    assert card.payload["card"]["task_id"] == f"personal:{scope.task.id}"
    shown = body_text(card.payload["card"])
    # 每一项按顺序完整展示：平台按参数生成的正文，含被回复的原话与要发出的内容。
    assert shown.index("1. ") < shown.index("2. ")
    assert TEXT.split("。")[0] in shown and SECOND in shown and "周五能给报价吗？" in shown
    assert "确认：执行 2 个飞书操作" in json.dumps(card.payload["card"], ensure_ascii=False)


async def test_hidden_characters_on_the_card_are_escaped(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await present(db_session)
    hostile = {
        **reply(),
        "text": "好的" + chr(0x202E) + "伪造" + chr(0x2028) + "第二行" + chr(0x200B),
    }
    await stage(db_session, scope, hostile)
    assert await release(db_engine, scope) == batch.CARD
    (card,) = await cards(db_session)
    shown = body_text(card.payload["card"])
    # 方向控制与零宽字符转义成可见的 \uXXXX；行分隔符在正文里已断成带竖线的新一行。
    for raw, escaped in ((chr(0x202E), "\\u202E"), (chr(0x200B), "\\u200B")):
        assert raw not in shown and escaped in shown
    assert chr(0x2028) not in shown and "│ 第二行" in shown


async def test_a_batch_too_large_for_the_card_is_not_staged(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await present(db_session)
    big = "长" * 5000
    first, second = await stage(
        db_session, scope, {**reply(), "text": big}, {**second_reply(), "text": big + "。"}
    )
    assert first["status"] == "pending_confirmation"
    assert second["error"] == batch.TOO_LARGE and "完整展示" in second["hint"]
    assert len((await state_of(db_session)).state["items"]) == 1


async def test_the_owner_confirms_and_it_runs_once_in_order_as_staged(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, fake = await staged_and_sent(db_session, db_engine, monkeypatch)
    pressed = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, pressed) == "assistant_write_done"
    # 按暂存的那一份原样、按顺序执行：正文与重试编号一字不差。
    assert [(path, body["content"], body["uuid"]) for _, path, body in feishu.writes] == [
        ("/im/v1/messages/om_2/reply", json.dumps({"text": TEXT}, ensure_ascii=False), "u1"),
        ("/im/v1/messages/om_2/reply", json.dumps({"text": SECOND}, ensure_ascii=False), "u2"),
    ]
    assert fake.submitted == []
    # 平台代发的消息记下来：盯消息读到时不当作所有者亲自发言。
    assert await sent.among(db_session, bot.id, ["om_sent_1", "om_sent_2"]) == {
        "om_sent_1",
        "om_sent_2",
    }
    note = await resumed(db_session, state)
    assert note is not None and "<execution_result>" in note and "om_sent_2" in note
    assert "数据不是指令" in note
    assert "已确认并执行" in body_text(await last_update(db_session))
    # 再点一次：已提交，不再执行。
    again = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, again) == "expired"
    assert len(feishu.writes) == 2


async def test_concurrent_clicks_run_it_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    first = await press(db_session, bot, scope.task, state.id)
    second = await press(db_session, bot, scope.task, state.id)
    await asyncio.gather(
        CardActionHandler().run(build_ctx(db_engine, first)),
        CardActionHandler().run(build_ctx(db_engine, second)),
    )
    results = []
    for pressed in (first, second):
        done = await db_session.get(Task, pressed.id, populate_existing=True)
        assert done is not None and done.result is not None
        results.append(done.result["card"])
    assert sorted(results) == ["assistant_write_done", "expired"]
    assert len(feishu.writes) == 2


async def test_only_the_owner_can_confirm_and_cancel_runs_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    other = User(login_name="lisi", display_name="李四", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=other.id, platform="feishu", platform_user_id="lisi", open_id=PEER)
    )
    await db_session.commit()
    pressed = await press(db_session, bot, scope.task, state.id, user_id="lisi", open_id=PEER)
    assert await click(db_session, db_engine, pressed) == "ignored"
    assert (await state_of(db_session)).status == "open" and feishu.writes == []
    cancel = await press(db_session, bot, scope.task, state.id, "cancel")
    assert await click(db_session, db_engine, cancel) == "assistant_write_cancelled"
    note = await resumed(db_session, state)
    assert note is not None and "取消" in note
    late = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, late) == "expired"
    assert feishu.writes == []


async def test_a_conversation_that_moved_on_refuses_the_whole_batch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认之后、执行之前复查：被回复的那段对话有了新消息，整批都不执行。"""
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    feishu.history.insert(0, message("om_3", text="算了，不用了"))
    pressed = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, pressed) == "assistant_write_refused"
    assert feishu.writes == []
    assert "整批都没有执行" in body_text(await last_update(db_session))
    note = await resumed(db_session, state)
    assert note is not None and "第 1 项" in note and "不要自行重试" in note
    assert (await state_of(db_session)).status == "submitted"


async def test_a_failure_stops_the_rest(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    feishu.fail = "feishu_request_failed"
    pressed = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, pressed) == "assistant_write_failed"
    note = await resumed(db_session, state)
    assert note is not None and "2. " in note and "未执行" in note and "不要自行重试" in note


async def test_a_stopped_assistant_does_not_execute(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    pressed = await press(db_session, bot, scope.task, state.id)
    row = await db_session.get(Bot, bot.id)
    assert row is not None
    row.assistant_state = "deprovisioning"
    await db_session.commit()
    assert await click(db_session, db_engine, pressed) in ("assistant_write_unavailable", "ignored")
    assert feishu.writes == []


async def test_an_expired_card_can_be_resent_by_the_owner_with_the_same_content(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    assert systems_confirm.TTL == timedelta(minutes=60)
    state.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    late = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, late) == "expired"
    assert feishu.writes == [] and await resumed(db_session, state) is None
    expired = await last_update(db_session)
    assert "过期" in body_text(expired) and "重新发起" in json.dumps(expired, ensure_ascii=False)
    # 别人点「重新发起」无效。
    other = User(login_name="lisi", display_name="李四", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=other.id, platform="feishu", platform_user_id="lisi", open_id=PEER)
    )
    await db_session.commit()
    stranger = await press(
        db_session, bot, scope.task, state.id, systems_confirm.RESEND, user_id="lisi", open_id=PEER
    )
    assert await click(db_session, db_engine, stranger) == "ignored"
    assert len(await cards(db_session)) == 1
    again = await press(db_session, bot, scope.task, state.id, systems_confirm.RESEND)
    assert await click(db_session, db_engine, again) == "assistant_write_resent"
    first, resent = await cards(db_session)
    assert resent.dedupe_key == f"assistant-write:{state.id}:resend:1"
    assert body_text(resent.payload["card"]) == body_text(first.payload["card"])
    reloaded = await state_of(db_session)
    assert reloaded.status == "open" and reloaded.expires_at > utcnow() + timedelta(minutes=59)
    # 新卡上确认：照样复查后执行。
    pressed = await press(db_session, bot, scope.task, state.id)
    assert await click(db_session, db_engine, pressed) == "assistant_write_done"
    assert len(feishu.writes) == 2
    # 已确认的不能再重发。
    no_more = await press(db_session, bot, scope.task, state.id, systems_confirm.RESEND)
    assert await click(db_session, db_engine, no_more) == "ignored"
    assert len(await cards(db_session)) == 2


async def test_content_older_than_a_day_cannot_be_resent_and_is_purged(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, scope, state, feishu, _ = await staged_and_sent(db_session, db_engine, monkeypatch)
    (card,) = await cards(db_session)
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card_1"}
    await db_session.commit()
    now = utcnow() + timedelta(minutes=61)
    # 过期：清理任务把卡片改成「已过期」并带「重新发起」，内容保留到第一次发卡后 24 小时。
    async with make_session_factory(db_engine)() as session:
        assert await systems_confirm.sweep(session, now) == 1
        await session.commit()
    expired = await last_update(db_session)
    assert "重新发起" in json.dumps(expired, ensure_ascii=False)
    reloaded = await state_of(db_session)
    until = systems_confirm.keep_until(reloaded)
    assert reloaded.status == "expired" and until is not None and reloaded.expires_at == until
    # 过了 24 小时：卡片改成不能重发，批次连同暂存的内容删除。
    async with make_session_factory(db_engine)() as session:
        assert await systems_confirm.sweep(session, until + timedelta(seconds=1)) == 1
        await session.commit()
    gone = await last_update(db_session)
    assert "已经清理" in body_text(gone) and '"button"' not in json.dumps(gone)
    assert (
        await db_session.scalar(select(InteractionState).where(InteractionState.id == state.id))
        is None
    )
    assert feishu.writes == []


async def test_a_turn_that_did_not_end_well_drops_its_writes(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    _, _, scope = await present(db_session)
    await stage(db_session, scope, reply())
    assert await release(db_engine, scope, send=False) is None
    state = await state_of(db_session)
    assert state.status == "cancelled"
    # 没有确认卡，只有一句「这批没有执行」的说明。
    assert [card.dedupe_key for card in await cards(db_session)] == [
        f"assistant-write:{state.id}:dropped"
    ]
    assert feishu.writes == [] and fake.submitted == []


async def test_a_rerun_of_the_same_task_starts_over(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await present(db_session)
    await stage(db_session, scope, reply())
    task = await db_session.get(Task, scope.task.id)
    assert task is not None
    task.attempts += 1
    await db_session.commit()
    (out,) = await stage(db_session, replace(scope, task=task), second_reply())
    assert out["pending"] == 1
    items = (await state_of(db_session)).state["items"]
    assert [item["calls"][0]["arguments"]["uuid"] for item in items] == ["u2"]


async def test_ordinary_bots_and_unconfirmable_turns_do_not_stage(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, scope = await present(db_session)
    # 普通 AI 员工：个人工具照旧，不经这里。
    standard = Bot(id=uuid.uuid4(), kind="standard", platform="feishu", owner_id=scope.user_id)
    assert (
        await batch.propose(
            db_session,
            None,  # type: ignore[arg-type]
            replace(scope, bot=standard),
            "feishu_reply_message",
            reply(),
            cfg=CFG,
            files=None,
        )
        is None
    )
    row = await db_session.get(Bot, bot.id)
    assert row is not None
    # 不是所有者本人私聊打字发来的一轮（没有入站事件、也不是平台来源）：照旧当场提交审批单。
    scheduled = replace(scope, bot=row, event=None)
    assert (
        await batch.propose(
            db_session,
            None,  # type: ignore[arg-type]
            scheduled,
            "feishu_reply_message",
            reply(),
            cfg=CFG,
            files=None,
        )
        is None
    )
    assert await db_session.scalar(select(InteractionState)) is None


# 无人在场：一轮一张单 ---------------------------------------------------------------------------


async def away(session: AsyncSession) -> tuple[Bot, User, Scope]:
    """所有者不在场的一轮（平台来源）：暂存后在本轮结束时合成一张审批单。"""
    bot, owner, scope = await assistant_turn(session)
    return bot, owner, replace(scope, platform=True)


async def submitted(
    session: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[AssistantApproval], FakeFeishu, FakeTower, Scope]:
    feishu, fake = install(monkeypatch)
    _, _, scope = await away(session)
    first, second = await stage(session, scope, reply(), second_reply())
    assert first["status"] == second["status"] == "pending_approval"
    assert (
        second["staged"] is True and second["pending"] == 2 and "合成一张审批单" in second["hint"]
    )
    assert fake.submitted == []
    assert await release(engine, scope) == batch.TOWER
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .order_by(AssistantApproval.created_at, AssistantApproval.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    head = next(row for row in rows if row.id == row.batch_id)
    rows = await service.members(session, head, lock=False)
    return rows, feishu, fake, scope


async def test_unattended_writes_go_out_as_one_approval_listing_every_item(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, feishu, fake, _ = await submitted(db_session, db_engine, monkeypatch)
    (sent_one,) = fake.submitted
    assert len(rows) == 2 and {row.batch_id for row in rows} == {rows[0].id}
    assert sent_one["external_id"] == rows[0].external_id
    assert "共 2 项" in sent_one["content"] and "拒绝则整单都不执行" in sent_one["content"]
    assert sent_one["content"].index("【第 1 项 / 共 2 项】") < sent_one["content"].index(SECOND)
    assert sent_one["abstract"].startswith("2 项 · ")
    assert [row.status for row in rows] == ["pending", "pending"]
    assert [row.args_enc["item"] for row in rows] == [1, 2]
    assert await service.pending_count(db_session, rows[0].bot_id) == 1
    assert feishu.writes == []
    # 摘要里一张单一行，注明共几项。
    shown, total, _, sizes = await digest._pending_approvals(db_session, rows[0].bot_id)
    assert total == 1 and len(shown) == 1
    assert "共 2 项" in digest._approval_summary(shown[0], sizes[shown[0].id])
    # approval_list 两层：各项带着所在的单与位置。
    owner_scope = SimpleNamespace(bot=SimpleNamespace(id=rows[0].bot_id))
    listed = await service.listing(db_session, owner_scope)  # type: ignore[arg-type]
    assert {(item["batch_id"], item["item"], item["items"]) for item in listed["items"]} == {
        (str(rows[0].id), 1, 2),
        (str(rows[0].id), 2, 2),
    }


async def test_a_single_unattended_write_is_an_ordinary_approval(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await away(db_session)
    await stage(db_session, scope, reply())
    assert await release(db_engine, scope) == batch.TOWER
    row = await db_session.scalar(select(AssistantApproval))
    assert row is not None and row.batch_id is None and row.status == "pending"
    (sent_one,) = fake.submitted
    assert "共 1 项" not in sent_one["content"] and "【第" not in sent_one["content"]


async def test_approving_runs_the_items_in_order_once_and_reports_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, feishu, fake, _ = await submitted(db_session, db_engine, monkeypatch)
    head = rows[0]
    found = await service.decide(
        db_session,
        approval_id=str(head.approval_id),
        external_id=head.external_id,
        status="approved",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    assert found.known and found.queued
    (job,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    status = await execute.run(make_session_factory(db_engine), Cipher(MASTER), head.id, job, CFG)
    assert status == "settled"
    assert [body["uuid"] for _, _, body in feishu.writes] == ["u1", "u2"]
    reloaded = await service.members(db_session, head, lock=False)
    assert [row.status for row in reloaded] == ["executed", "executed"]
    # 核销的就是批准的那一份：Tower 按整张单的请求体记下的哈希对得上。
    assert len(fake.redeemed) == 1 and fake.reported == [(head.approval_id, "executed", None)]
    (result,) = await queued(db_session, APPROVAL_RESULT_KIND)
    assert result.payload["approval_id"] == str(head.id)
    text_ = service.batch_text(reloaded)
    assert (
        "2 项是同一张审批单" in text_ and text_.count("已批准，平台已按提交时的参数原样执行") == 2
    )
    # 执行任务重跑不再执行一次。
    again = await execute.run(make_session_factory(db_engine), Cipher(MASTER), head.id, job, CFG)
    assert again == "settled" and len(feishu.writes) == 2


async def test_a_failing_item_stops_the_rest_and_earlier_ones_stay(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, feishu, fake, _ = await submitted(db_session, db_engine, monkeypatch)
    head = rows[0]
    await service.decide(
        db_session,
        approval_id=str(head.approval_id),
        external_id=head.external_id,
        status="approved",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    (job,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    feishu.fail = "feishu_request_failed"
    await execute.run(make_session_factory(db_engine), Cipher(MASTER), head.id, job, CFG)
    reloaded = await service.members(db_session, head, lock=False)
    assert [(row.status, row.error_code) for row in reloaded] == [
        ("failed", "feishu_request_failed"),
        ("failed", service.SKIPPED),
    ]
    assert fake.reported == [(head.approval_id, "failed", "feishu_request_failed")]
    assert "这一项没有执行" in service.batch_text(reloaded)


async def test_rejecting_runs_nothing_and_comes_back_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, feishu, _, _ = await submitted(db_session, db_engine, monkeypatch)
    head = rows[0]
    found = await service.decide(
        db_session,
        approval_id=str(head.approval_id),
        external_id=head.external_id,
        status="rejected",
        reason="先别发",
        decided_at=None,
    )
    await db_session.commit()
    assert found.queued
    reloaded = await service.members(db_session, head, lock=False)
    assert [row.status for row in reloaded] == ["rejected", "rejected"]
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    assert len(await queued(db_session, APPROVAL_RESULT_KIND)) == 1
    assert feishu.writes == []


async def test_withdrawing_one_item_keeps_the_approval_and_skips_only_that_item(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, feishu, fake, scope = await submitted(db_session, db_engine, monkeypatch)
    head, other = rows
    out = await service.withdraw(db_session, scope, str(other.id), "不用了", cfg=CFG)
    await db_session.commit()
    assert out["status"] == "withdrawn" and "其余的项仍在待批" in out["note"]
    assert fake.withdrawn == []
    await service.decide(
        db_session,
        approval_id=str(head.approval_id),
        external_id=head.external_id,
        status="approved",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    (job,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    await execute.run(make_session_factory(db_engine), Cipher(MASTER), head.id, job, CFG)
    reloaded = await service.members(db_session, head, lock=False)
    assert [row.status for row in reloaded] == ["executed", "withdrawn"]
    assert [body["uuid"] for _, _, body in feishu.writes] == ["u1"]
    assert len(fake.redeemed) == 1


async def test_withdrawing_every_item_withdraws_the_approval_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, _, fake, scope = await submitted(db_session, db_engine, monkeypatch)
    for row in rows:
        await service.withdraw(db_session, scope, str(row.id), "", cfg=CFG)
        await db_session.commit()
    assert fake.withdrawn == [rows[0].external_id]


async def test_an_expired_batch_comes_back_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, _, fake, _ = await submitted(db_session, db_engine, monkeypatch)
    assert await service.expire(db_session, utcnow() + service.TTL + timedelta(seconds=1), CFG) == 2
    await db_session.commit()
    reloaded = await service.members(db_session, rows[0], lock=False)
    assert [row.status for row in reloaded] == ["expired", "expired"]
    assert fake.withdrawn == [rows[0].external_id]
    (result,) = await queued(db_session, APPROVAL_RESULT_KIND)
    assert result.payload == {**result.payload, "approval_id": str(rows[0].id), "status": "expired"}


async def test_without_an_approval_service_unattended_writes_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, _ = install(monkeypatch)
    _, _, scope = await away(db_session)
    out = await batch.propose(
        db_session,
        None,
        scope,
        "feishu_reply_message",
        reply(),
        cfg=None,
        files=None,  # type: ignore[arg-type]
    )
    assert out is not None and out["error"] == "approval_service_unavailable"
    assert feishu.writes == [] and await db_session.scalar(select(InteractionState)) is None


async def test_an_unfinished_unattended_turn_submits_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await away(db_session)
    await stage(db_session, scope, reply(), second_reply())
    assert await release(db_engine, scope, send=False) is None
    assert fake.submitted == [] and await db_session.scalar(select(AssistantApproval)) is None
    # 收尾没走到的批次：过了期限由清理任务删掉，不会悬着，也不会再提交。
    _, _, other = await away_again(db_session, scope)
    await stage(db_session, other, reply())
    async with make_session_factory(db_engine)() as session:
        swept = await systems_confirm.sweep(session, utcnow() + timedelta(days=1))
        await session.commit()
    assert swept == 2 and await db_session.scalar(select(InteractionState)) is None
    assert fake.submitted == []


async def away_again(session: AsyncSession, scope: Scope) -> tuple[None, None, Scope]:
    """同一位助理另一轮无人在场的轮次（换一个任务号）。"""
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=scope.bot.id,
            kind="chat",
            user_id=scope.user_id,
            session_key=scope.task.session_key,
            inbound_event_id=scope.task.inbound_event_id,
            dedupe_key=f"again:{uuid.uuid4()}",
            payload={},
        ),
    )
    assert task is not None
    await session.commit()
    return None, None, replace(scope, task=task)


async def test_unsubmitted_items_come_back_to_the_assistant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await away(db_session)
    await stage(db_session, scope, reply(), second_reply())
    fake.down = True
    assert await release(db_engine, scope) == batch.TOWER
    rows = list(await db_session.scalars(select(AssistantApproval)))
    await sealed.open_rows(db_session, rows)
    assert {(row.status, row.error_code) for row in rows} == {
        ("failed", service.UNSUBMITTED + service.UNAVAILABLE)
    }
    (result,) = await queued(db_session, APPROVAL_RESULT_KIND)
    head = next(row for row in rows if row.id == row.batch_id)
    assert "没能提交审批" in service.batch_text(await service.members(db_session, head, lock=False))
    assert result.payload["approval_id"] == str(head.id)


async def test_an_unattended_turn_never_gets_a_card_even_with_the_owners_event(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批结果回合沿用所有者私聊那一轮的入站事件，照样无人在场：服务端按任务类型判，只能
    合成审批单，不出确认卡；伪造的点击也执行不了它。"""
    feishu, fake = install(monkeypatch)
    bot, _, scope = await present(db_session)
    await db_session.execute(
        text("UPDATE tasks SET kind = :kind WHERE id = :id"),
        {"kind": APPROVAL_RESULT_KIND, "id": scope.task.id},
    )
    await db_session.commit()
    task = await db_session.get(Task, scope.task.id, populate_existing=True)
    assert task is not None
    unattended = replace(scope, task=task)
    assert unattended.unattended and unattended.event is not None
    (out,) = await stage(db_session, unattended, reply())
    assert out["status"] == "pending_approval" and out["staged"] is True
    state = await state_of(db_session)
    assert state.state["mode"] == batch.TOWER
    pressed = await press_without_card(db_session, bot, task, state.id)
    assert await click(db_session, db_engine, pressed) == "ignored"
    assert feishu.writes == [] and fake.submitted == []
    assert await release(db_engine, unattended) == batch.TOWER
    assert len(fake.submitted) == 1
    assert [card.dedupe_key for card in await cards(db_session)] == [f"forged:{state.id}"]


async def press_without_card(
    session: AsyncSession, bot: Bot, origin: Task, state_id: uuid.UUID
) -> Task:
    """伪造一张「卡片」让点击能进到处理器：出站里放一条看起来发过的卡。"""
    session.add(
        OutboxItem(
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"forged:{state_id}",
            target={"chat_id": CHAT},
            payload={"card": {"task_id": f"personal:{origin.id}"}, "_feishu_message_id": "om_x"},
            status="sent",
        )
    )
    await session.commit()
    return await press(session, bot, origin, state_id)


# 审查后的补充 ---------------------------------------------------------------------------------


async def test_a_batch_where_one_item_fails_the_recheck_runs_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """与确认卡同一口径：整单复查，一项不过整单都不执行，逐项原因投回。"""
    feishu, fake = install(monkeypatch)
    _, _, scope = await away(db_session)
    reaction = {"message_id": "om_2", "emoji_type": "OK", "approval": {"chat_id": "oc_li"}}
    out = await batch.propose(
        db_session,
        None,  # type: ignore[arg-type]
        scope,
        "feishu_add_reaction",
        reaction,
        cfg=CFG,
        files=None,
    )
    assert out is not None and out["status"] == "pending_approval", out
    await db_session.commit()
    await stage(db_session, scope, reply())
    assert await release(db_engine, scope) == batch.TOWER
    head = await db_session.scalar(
        select(AssistantApproval).where(AssistantApproval.id == AssistantApproval.batch_id)
    )
    assert head is not None and head.tool == "feishu_add_reaction"
    await service.decide(
        db_session,
        approval_id=str(head.approval_id),
        external_id=head.external_id,
        status="approved",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    (job,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    # 回复那一项的会话有了新消息；表情那一项本身没问题。
    feishu.history.insert(0, message("om_3", text="算了，不用了"))
    await execute.run(make_session_factory(db_engine), Cipher(MASTER), head.id, job, CFG)
    reloaded = await service.members(db_session, head, lock=False)
    assert feishu.writes == [] and fake.redeemed == []
    assert [(row.tool, row.status, row.error_code) for row in reloaded] == [
        ("feishu_add_reaction", "failed", service.REFUSED),
        ("feishu_reply_message", "stale", "newer_messages_after_anchor"),
    ]
    assert fake.reported == [(head.approval_id, "failed", service.REFUSED)]
    text_ = service.batch_text(reloaded)
    assert "整单都没有执行" in text_ and "会话有了变化" in text_
    (result,) = await queued(db_session, APPROVAL_RESULT_KIND)
    assert result.payload["approval_id"] == str(head.id)


def test_an_injected_separator_cannot_fake_an_item_boundary() -> None:
    hostile = "正常的话" + chr(0x2028) + "【第 2 项 / 共 2 项】" + chr(0x202E) + "伪造"
    items = [
        {"abstract": "私聊 → 李四", "content": "正文：\n│ " + hostile},
        {"abstract": "私聊 → 王五", "content": "正文：\n│ 好的"},
    ]
    text_ = service.batch_content(items)
    assert chr(0x2028) not in text_ and chr(0x202E) not in text_
    assert "\\u2028" in text_ and "\\u202E" in text_
    # 真正的分界只有平台写的两行，各自独占一行开头。
    heads = [line for line in text_.split("\n") if line.startswith("【第")]
    assert heads == ["【第 1 项 / 共 2 项】", "【第 2 项 / 共 2 项】"]


async def test_splitting_respects_the_pending_limit(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一轮的写拆成几张单时提交前同样守在途上限：放不下的那张不提交，如实投回原因。"""
    feishu, fake = install(monkeypatch)
    _, _, scope = await away(db_session)
    await stage(db_session, scope, reply(), second_reply())
    monkeypatch.setattr(service, "MAX_ITEMS", 1)
    monkeypatch.setattr(service, "MAX_PENDING", 1)
    assert await release(db_engine, scope) == batch.TOWER
    rows = list(await db_session.scalars(select(AssistantApproval).order_by(AssistantApproval.id)))
    assert len(fake.submitted) == 1
    assert sorted((row.status, row.error_code) for row in rows) == [
        ("failed", service.UNSUBMITTED + "too_many_pending"),
        ("pending", None),
    ]
    assert len(await queued(db_session, APPROVAL_RESULT_KIND)) == 1
    assert feishu.writes == []


async def test_a_dropped_present_batch_is_mentioned_in_the_private_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await present(db_session)
    await stage(db_session, scope, reply())
    assert await release(db_engine, scope, send=False) is None
    (notice,) = await cards(db_session)
    assert notice.target == {"chat_id": CHAT}
    shown = body_text(notice.payload["card"])
    assert "没有发确认卡，也没有执行" in shown and "私聊 → 李四" in shown


async def test_a_dropped_unattended_batch_becomes_a_digest_report(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.db.models import AssistantReport

    install(monkeypatch)
    _, _, scope = await away(db_session)
    await stage(db_session, scope, reply(), second_reply())
    assert await release(db_engine, scope, send=False) is None
    reports = list(await db_session.scalars(select(AssistantReport)))
    await sealed.open_rows(db_session, reports)
    (report,) = reports
    assert "2 个飞书操作没有提交审批" in report.text_enc and report.urgent is False
    assert await cards(db_session) == []
    # 收尾出错、没交出去的批次：清理任务删掉之前同样留下说明。
    _, _, other = await away_again(db_session, scope)
    await stage(db_session, other, reply())
    async with make_session_factory(db_engine)() as session:
        await systems_confirm.sweep(session, utcnow() + timedelta(days=1))
        await session.commit()
    reports = list(await db_session.scalars(select(AssistantReport)))
    assert len(reports) == 2


async def test_the_three_releases_do_not_depend_on_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前一种批次交出时出错，后面的照样交出。"""
    from coreman.core.cloud_proxy import confirm as cloud_confirm
    from coreman.runtime.worker.chat.finalize import FinalizeStage
    from coreman.runtime.worker.chat.models import Verdict

    called: list[str] = []

    async def broken(*args: Any, **kw: Any) -> bool:
        called.append("systems")
        raise RuntimeError("boom")

    async def cloud(*args: Any, **kw: Any) -> bool:
        called.append("cloud")
        raise RuntimeError("boom")

    async def feishu(*args: Any, **kw: Any) -> str | None:
        called.append("feishu")
        return None

    monkeypatch.setattr(systems_confirm, "release", broken)
    monkeypatch.setattr(cloud_confirm, "release", cloud)
    monkeypatch.setattr(batch, "release", feishu)

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def commit(self) -> None:
            return None

    class Log:
        def info(self, *a: Any, **k: Any) -> None:
            return None

        warning = info

    ctx = SimpleNamespace(
        session_factory=Session, cipher=None, task=SimpleNamespace(id=1), log=Log()
    )
    pre = SimpleNamespace(
        intake=SimpleNamespace(bot=SimpleNamespace(id=uuid.uuid4(), kind="assistant"))
    )
    verdict = Verdict("success", "succeeded", None, None, "好")
    stage_ = FinalizeStage.__new__(FinalizeStage)
    await stage_._release_writes(ctx, pre, verdict)  # type: ignore[arg-type]
    assert called == ["systems", "cloud", "feishu"]
