"""群里发起的事：进展卡原地更新，提问卡发在群里、只有被问的人能答，回复末尾写任务的状态。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from coreman.core.db.models import (
    Bot,
    InboundEvent,
    OutboxItem,
    User,
    UserIdentity,
    WorkTask,
)
from coreman.core.wecom.messages import InboundMessage, Sender
from coreman.core.work_tasks import agent_tools, cards, flow, progress
from coreman.runtime.gateway_feishu import reply_buttons
from coreman.runtime.worker import work_task_cards
from coreman.runtime.worker.chat import work
from tests.integration.worker_helpers import seed_bot

BASE = "https://coreman.example.com"
GROUP = "oc_group"


async def _person(db_session, name: str) -> User:
    user = User(login_name=name, display_name=name, email=f"{name}@example.test")
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=f"ou_{name}"))
    return user


async def _setup(db_session, *, source: str = "dispatch"):
    lead, _, _ = await seed_bot(db_session)
    lead.platform, lead.name = "feishu", "调度员"
    helper = Bot(
        bot_key="data_bot",
        platform="feishu",
        name="数据助手",
        created_by=lead.created_by,
        relay_server_id=lead.relay_server_id,
        model=lead.model,
        working_dir="/data/skills/data_bot",
        system_prompt="你是数据助手",
        merged_system_prompt="你是数据助手",
        credentials_enc=lead.credentials_enc,
        env_vars_enc=lead.env_vars_enc,
    )
    db_session.add(helper)
    alice = await _person(db_session, "alice")
    origin = InboundEvent(
        bot_id=lead.id,
        platform="feishu",
        platform_msg_id="om1",
        kind="message",
        chat_type="group",
        chat_id=GROUP,
        sender_platform_user_id="ou_alice",
        sender_open_id="ou_open_alice",
        payload={},
        reply_context={"chat_id": GROUP, "message_id": "om1"},
    )
    db_session.add(origin)
    await db_session.flush()
    root = WorkTask(
        title="订单与仓储系统对接",
        status="in_progress",
        source=source,
        platform="feishu",
        chat_id=GROUP,
        origin_event_id=origin.id,
        requester_user_id=alice.id,
        requester_name="alice",
        principal_user_id=alice.id,
        assignee_bot_id=lead.id,
        dispatcher_bot_id=lead.id,
        tracked=True,
    )
    db_session.add(root)
    await db_session.flush()
    root.root_id = root.id
    await db_session.commit()
    return lead, helper, alice, root


async def _tool(db_session, root, lead, name: str, args: dict):
    made = await agent_tools.invoke(
        db_session, root, lead, name, args, chat_task_id=1, base_url=BASE
    )
    await db_session.commit()
    return made


def _subtask(helper, title: str) -> dict:
    return {"title": title, "brief": "要求写全", "assignee": {"type": "bot", "id": str(helper.id)}}


def _body(item: OutboxItem) -> str:
    return str(item.payload["card"]["body"]["elements"][0]["content"])


async def _progress_items(db_session, root) -> list[OutboxItem]:
    return list(
        await db_session.scalars(
            select(OutboxItem)
            .where(OutboxItem.dedupe_key.like(f"work-task:{root.id}:progress%"))
            .order_by(OutboxItem.id)
            .execution_options(populate_existing=True)
        )
    )


async def test_group_task_keeps_one_progress_card_up_to_date(db_session):
    lead, helper, _, root = await _setup(db_session)
    first = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    (card,) = await _progress_items(db_session, root)
    # 由执行员工发在来源群里，回复在发起人的那条消息下面。
    assert card.bot_id == lead.id and card.kind == "send"
    assert card.target == {"chat_id": GROUP, "message_id": "om1"} and card.payload["_reply"]
    assert _body(card) == "⏳ 对齐接口文档 — 数据助手"
    header = card.payload["card"]["header"]
    assert header["subtitle"]["content"] == "进行中 · 已完成 0/1 件"

    # 卡片还没发出去时又拆了一件：直接换成最新内容，不多发一张。
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "仓储侧开发"))
    (card,) = await _progress_items(db_session, root)
    assert _body(card) == "⏳ 对齐接口文档 — 数据助手\n⏳ 仓储侧开发 — 数据助手"

    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == first["code"]))
    child.status, child.summary = "done", "接口文档已合并"
    await flow.settle(
        db_session,
        child,
        before_ask=None,
        before_status="in_progress",
        actor_id=None,
        base_url=BASE,
    )
    await db_session.commit()
    _, update = await _progress_items(db_session, root)
    assert update.kind == "card_update" and update.target["message_id"] == "om_card"
    assert _body(update).splitlines()[0] == "✅ 对齐接口文档 — 数据助手：接口文档已合并"
    assert update.payload["card"]["header"]["subtitle"]["content"] == "进行中 · 已完成 1/2 件"

    # 状态没变不重复更新。
    await progress.refresh(db_session, child, base_url=BASE)
    await db_session.commit()
    assert len(await _progress_items(db_session, root)) == 2


async def test_tasks_not_started_in_a_group_get_no_progress_card(db_session):
    lead, helper, _, root = await _setup(db_session, source="web")
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    assert await _progress_items(db_session, root) == []


async def test_questions_go_to_the_group_and_only_the_asked_person_answers(db_session):
    lead, _, alice, root = await _setup(db_session)
    asked = await _tool(
        db_session,
        root,
        lead,
        "ask_human",
        {"question": "退货单作废还是放宽校验？", "options": ["作废", "放宽"], "context": "背景"},
    )
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == asked["code"]))
    item = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{child.id}:ask:%"))
    )
    # 发在来源群里、@ 被问的人；按钮带着能回答的人。
    assert item.bot_id == lead.id and item.target == {"chat_id": GROUP, "message_id": "om1"}
    elements = item.payload["card"]["body"]["elements"]
    assert elements[0]["content"] == "<at id=ou_open_alice></at> 请你来定"
    button = next(e for e in elements if e.get("tag") == "button")
    assert button["behaviors"][0]["value"]["allowed"] == ["ou_alice"]
    # 进展卡上写明在等谁。
    (card,) = await _progress_items(db_session, root)
    assert _body(card) == "🟡 等 **alice** 回答：退货单作废还是放宽校验？"
    assert card.payload["card"]["header"]["subtitle"]["content"].startswith("等人处理")

    # 别人点了：worker 不改卡片，问题还在。
    await _person(db_session, "bob")
    clicked = InboundEvent(
        bot_id=lead.id,
        platform="feishu",
        platform_msg_id="action:evt-bob",
        kind="card_action",
        chat_type="group",
        chat_id=GROUP,
        sender_platform_user_id="ou_bob",
        payload={},
        reply_context={"chat_id": GROUP, "message_id": "om_ask"},
    )
    db_session.add(clicked)
    await db_session.flush()
    action = {
        "task_id": cards.card_task_id(child),
        "event_key": cards.EVENT_KEY,
        "level": f"{child.ask['id']}:o1",
        "selected": {},
    }
    result = await work_task_cards.handle(
        db_session, SimpleNamespace(public_base_url=BASE), lead, clicked, action
    )
    await db_session.commit()
    assert result == "not_allowed" and child.ask is not None and child.status != "done"
    updates = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"{clicked.id}:card_update")
    )
    assert list(updates) == []
    assert alice.id is not None


async def test_someone_who_never_spoke_in_the_group_is_asked_privately(db_session):
    lead, _, _, root = await _setup(db_session)
    carol = await _person(db_session, "carol")
    asked = await _tool(
        db_session,
        root,
        lead,
        "ask_human",
        {
            "question": "仓库地址以哪份为准？",
            "options": ["订单系统的", "仓储系统的"],
            "context": "背景",
            "to_user_id": str(carol.id),
        },
    )
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == asked["code"]))
    item = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{child.id}:ask:%"))
    )
    assert item.target == {"user_id": "ou_carol"}
    button = next(e for e in item.payload["card"]["body"]["elements"] if e.get("tag") == "button")
    assert "allowed" not in button["behaviors"][0]["value"]


@pytest.mark.parametrize(("sender", "refused"), [("ou_bob", True), ("ou_alice", False)])
def test_gateway_tells_others_the_question_is_not_theirs(sender, refused):
    message = InboundMessage(
        platform="feishu",
        bot_id="00000000-0000-0000-0000-000000000001",
        kind="card_action",
        chat_type="group",
        chat_id=GROUP,
        sender=Sender(platform_user_id=sender),
        message_id="action:evt",
        card_action={
            "task_id": "personal:1",
            "event_key": cards.EVENT_KEY,
            "allowed": ["ou_alice"],
        },
        reply_context={},
    )
    assert reply_buttons._not_asked(message) is refused
    # 私聊里的卡没有名单：照常处理。
    message.card_action = {"task_id": "personal:1", "event_key": cards.EVENT_KEY}
    assert reply_buttons._not_asked(message) is False


async def test_reply_ends_with_the_state_of_the_task(db_session):
    lead, helper, _, root = await _setup(db_session)
    made = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    await _tool(db_session, root, lead, "wait_for", {})
    assert await work.state_line(db_session, root) == "⏳ 进行中 · 在等数据助手，有结果我接着安排"
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "仓储侧开发"))
    await _tool(db_session, root, lead, "wait_for", {})
    line = await work.state_line(db_session, root)
    assert line == "⏳ 进行中 · 在等数据助手的 2 件事，有结果我接着安排"
    root.status, root.waiting_on = "waiting", {"kind": "human", "human_text": "等确认：合并 PR"}
    assert await work.state_line(db_session, root) == "🟡 等alice确认"
    root.status, root.waiting_on = "done", None
    assert await work.state_line(db_session, root) == "✅ 已完成"
    root.tracked = False
    assert await work.state_line(db_session, root) is None
    assert made["code"]
