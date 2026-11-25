"""任务平台的飞书卡片：派给人时推一张可点选的卡片，卡片上回答和网页上回答是同一个问题。"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from coreman.core.db.models import (
    InboundEvent,
    OutboxItem,
    Setting,
    User,
    UserIdentity,
    WorkTask,
)
from coreman.core.work_tasks import cards, service
from coreman.runtime.worker import work_task_cards
from tests.integration.worker_helpers import seed_bot

BASE = "https://coreman.example.com"


async def _person(db_session, name: str) -> User:
    user = User(login_name=name, display_name=name, email=f"{name}@example.test")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=f"ou_{name}"))
    return user


async def _click(db_session, bot, row, *, user: str, level: str, text: str = "") -> str:
    inbound = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{level}:{user}:{text}",
        kind="card_action",
        chat_type="group",
        chat_id="oc_dm_bob",
        sender_platform_user_id=f"ou_{user}",
        payload={},
        reply_context={"chat_id": "oc_dm_bob", "message_id": "om_card"},
    )
    db_session.add(inbound)
    await db_session.flush()
    action = {
        "task_id": cards.card_task_id(row),
        "event_key": cards.EVENT_KEY,
        "level": level,
        "selected": {cards.TEXT_FIELD: [text]} if text else {},
    }
    ctx = SimpleNamespace(public_base_url=BASE)
    result = await work_task_cards.handle(db_session, ctx, bot, inbound, action)
    await db_session.commit()
    return result


async def test_assigning_a_person_sends_a_card_that_answers_like_the_web(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice, bob = await _person(db_session, "alice"), await _person(db_session, "bob")
    db_session.add(Setting(key=cards.NOTIFIER_SETTING, value=str(bot.id)))
    row = await service.create(
        db_session, alice, title="整理供应商名单", assignee=service.Assignee("human", bob.id)
    )
    await cards.sync(
        db_session, row, before_ask=None, before_status="", actor_id=alice.id, base_url=BASE
    )
    await db_session.commit()

    sent = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert sent.target == {"user_id": "ou_bob"}
    card = sent.payload["card"]
    assert card["task_id"] == f"personal:{row.id}"
    flat = str(card)
    assert "接受" in flat and "拒绝" in flat and "其他" in flat
    assert f"{BASE}/work-tasks/{row.code}" in flat
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": "om_card"}
    await db_session.commit()

    ask_id = row.ask["id"]
    # 拒绝没写理由：卡片不动，让他补上。
    assert await _click(db_session, bot, row, user="bob", level=f"{ask_id}:decline") == "needs_text"
    assert await _click(db_session, bot, row, user="bob", level=f"{ask_id}:accept") == "answered"
    row = await db_session.scalar(
        select(WorkTask).where(WorkTask.id == row.id).execution_options(populate_existing=True)
    )
    assert row.status == "in_progress" and row.ask is None
    closed = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"work-task:{row.id}:ask:{ask_id}:closed")
    )
    assert closed.target["message_id"] == "om_card" and "bob：接受" in str(closed.payload["card"])
    # 网页上已经答过了，卡片上再点：卡片改成现状。
    assert await _click(db_session, bot, row, user="bob", level=f"{ask_id}:decline", text="x") == (
        "stale"
    )


async def test_finishing_notifies_the_requester(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice, bob = await _person(db_session, "alice"), await _person(db_session, "bob")
    db_session.add(Setting(key=cards.NOTIFIER_SETTING, value=str(bot.id)))
    row = await service.create(
        db_session, alice, title="订团建场地", assignee=service.Assignee("human", bob.id)
    )
    row.status, row.ask = "in_progress", None
    await db_session.flush()
    before = row.status
    await service.complete(db_session, row, bob, "订好了 A 场地")
    await cards.sync(
        db_session, row, before_ask=None, before_status=before, actor_id=bob.id, base_url=BASE
    )
    await db_session.commit()
    notes = list(await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send")))
    assert [n.target for n in notes] == [{"user_id": "ou_alice"}]
    assert "已完成：订好了 A 场地" in str(notes[0].payload["card"])
