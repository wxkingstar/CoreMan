"""群里发起的事：进展卡原地更新，提问卡发在群里、只有被问的人能答，回复末尾写任务的状态。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
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
    WorkTaskEvent,
    WorkTaskWatcher,
)
from coreman.core.wecom.messages import InboundMessage, Sender
from coreman.core.work_tasks import agent_tools, cards, due, flow, progress
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


def _clean(text: str) -> str:
    """时间标签按看的人的时区显示，这里统一换成「时:分」好比对。"""
    return re.sub(r"<local_datetime[^>]*></local_datetime>", "时:分", text)


def _walk(elements: list[dict]) -> list[dict]:
    """按从上到下、从左到右的顺序摊平：并排的那一行、表单里的东西也算。"""
    out: list[dict] = []
    for element in elements:
        if element.get("tag") == "column_set":
            for column in element["columns"]:
                out += _walk(column["elements"])
        elif element.get("tag") == "form":
            out += _walk(element["elements"])
        else:
            out.append(element)
    return out


def _buttons(item: OutboxItem) -> list[dict]:
    elements = _walk(item.payload["card"]["body"]["elements"])
    return [e for e in elements if e.get("tag") == "button"]


def _blocks(item: OutboxItem) -> list[str]:
    """卡上一块一块的文字（最后那行任务页链接不算）；按钮写成 [文字]，输入框写成 〔提示〕。"""
    out: list[str] = []
    for element in _walk(item.payload["card"]["body"]["elements"]):
        if element.get("tag") == "markdown":
            out.append(_clean(str(element["content"])))
        elif element.get("tag") == "div":
            out.append(str(element["text"]["content"]))
        elif element.get("tag") == "button":
            out.append(f"[{element['text']['content']}]")
        elif element.get("tag") == "input":
            out.append(f"〔{element['placeholder']['content']}〕")
    return out[:-1]


def _subtitle(item: OutboxItem) -> str:
    return str(item.payload["card"]["header"]["subtitle"]["content"])


async def _progress_items(db_session, root) -> list[OutboxItem]:
    return list(
        await db_session.scalars(
            select(OutboxItem)
            .where(OutboxItem.dedupe_key.like(f"work-task:{root.id}:progress%"))
            .order_by(OutboxItem.id)
            .execution_options(populate_existing=True)
        )
    )


async def _mark_sent(db_session, item: OutboxItem, message_id: str) -> None:
    item.status = "sent"
    item.sent_at = datetime.now(UTC) - timedelta(seconds=30)
    item.payload = {**item.payload, "_feishu_message_id": message_id}
    await db_session.commit()


async def _chatter(db_session, lead, count: int) -> None:
    """群里又来了几条别人的消息。"""
    for n in range(count):
        db_session.add(
            InboundEvent(
                bot_id=lead.id,
                platform="feishu",
                platform_msg_id=f"om_chat_{n}",
                kind="message",
                chat_type="group",
                chat_id=GROUP,
                sender_platform_user_id="ou_someone",
                payload={},
                reply_context={},
            )
        )
    await db_session.commit()


async def _finish(db_session, child: WorkTask, summary: str) -> None:
    child.status, child.summary = "done", summary
    child.claimed_at = datetime.now(UTC) - timedelta(minutes=6, seconds=5)
    child.closed_at = datetime.now(UTC)
    await flow.settle(
        db_session,
        child,
        before_ask=None,
        before_status="in_progress",
        actor_id=None,
        base_url=BASE,
    )
    await db_session.commit()


async def test_one_card_shows_the_whole_picture_and_is_updated_in_place(db_session):
    lead, helper, _, root = await _setup(db_session)
    root.summary = "两个系统对齐接口并联调"
    first = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    (card,) = await _progress_items(db_session, root)
    # 由这个群的调度员发在来源群里，回复在发起人的那条消息下面。
    assert card.bot_id == lead.id and card.kind == "send"
    assert card.target == {"chat_id": GROUP, "message_id": "om1"} and card.payload["_reply"]
    assert _blocks(card) == [
        "**目标**：两个系统对齐接口并联调",
        "**正在做**\n⏳ 对齐接口文档 — 数据助手 · 时:分 开始",
    ]
    assert _subtitle(card) == "进行中 □□□□□□□□ 0/1"

    # 卡片还没发出去时又拆了一件、写了后面的步骤和一句进展：直接换成最新内容，不多发一张。
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "仓储侧开发"))
    await _tool(
        db_session,
        root,
        lead,
        "update_task",
        {
            "detail": "计划",
            "human_text": "接口文档在对，仓储侧同时开工",
            "plan": ["两边在测试环境联调", "仓储侧开发"],
        },
    )
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == first["code"]))
    await _tool(
        db_session,
        child,
        helper,
        "update_task",
        {"detail": "细节", "human_text": "正在等测试环境发布"},
    )
    (card,) = await _progress_items(db_session, root)
    assert _blocks(card) == [
        "**目标**：两个系统对齐接口并联调",
        "**正在做**\n"
        "⏳ 对齐接口文档 — 数据助手 · 时:分 开始，正在等测试环境发布（时:分）\n"
        "⏳ 仓储侧开发 — 数据助手 · 时:分 开始",
        "**接下来**\n☐ 两边在测试环境联调",  # 已经拆成子任务的那一步不再列
        "<font color='grey'>最新：接口文档在对，仓储侧同时开工（时:分）</font>",
    ]
    assert _subtitle(card) == "进行中 □□□□□□□□ 0/3"

    # 发出去之后做完一步：卡片后面没隔几条消息，原地更新。
    await _mark_sent(db_session, card, "om_card")
    await _finish(db_session, child, "接口文档已合并")
    _, update = await _progress_items(db_session, root)
    assert update.kind == "card_update" and update.target["message_id"] == "om_card"
    assert _blocks(update)[1:4] == [
        "**正在做**\n⏳ 仓储侧开发 — 数据助手 · 时:分 开始",
        "**接下来**\n☐ 两边在测试环境联调",
        "**已完成（1）**\n✅ 对齐接口文档 — 数据助手（用时 6 分钟）",
    ]
    assert _subtitle(update) == "进行中 ■■■□□□□□ 1/3"

    # 状态没变不重复更新。
    await progress.refresh(db_session, child, base_url=BASE)
    await db_session.commit()
    assert len(await _progress_items(db_session, root)) == 2


async def test_a_buried_card_moves_to_the_bottom_when_something_real_changes(db_session):
    lead, helper, _, root = await _setup(db_session)
    first = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    second = await _tool(db_session, root, lead, "create_task", _subtask(helper, "仓储侧开发"))
    (card,) = await _progress_items(db_session, root)
    await _mark_sent(db_session, card, "om_card")
    await _chatter(db_session, lead, progress.BURIED_AFTER)

    # 只是多了一句进展：不算实质变化，被顶上去了也原地更新。
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == first["code"]))
    await _tool(
        db_session, child, helper, "update_task", {"detail": "细节", "human_text": "正在等发布"}
    )
    _, update = await _progress_items(db_session, root)
    assert update.kind == "card_update" and update.target["message_id"] == "om_card"

    # 做完一步、卡片后面又隔了十条消息：新发一张到最新的位置，旧的缩成一行。
    await _finish(db_session, child, "接口文档已合并")
    items = await _progress_items(db_session, root)
    moved, stub = items[-2], items[-1]
    assert moved.kind == "send" and moved.target == {"chat_id": GROUP, "message_id": "om1"}
    assert "**已完成（1）**" in _blocks(moved)[-1]
    assert stub.kind == "card_update" and stub.target["message_id"] == "om_card"
    assert _subtitle(stub) == "这张清单已更新，最新的在下面"
    assert "task_id" not in stub.payload["card"]  # 旧卡上没有按钮了

    # 新的那张发出去之前再有变化：换它的内容，不再多发。
    other = await db_session.scalar(select(WorkTask).where(WorkTask.code == second["code"]))
    await _finish(db_session, other, "仓储侧已上线测试环境")
    again = await _progress_items(db_session, root)
    assert len(again) == len(items) and "**已完成（2）**" in _blocks(again[-2])[-1]


async def test_tasks_not_started_in_a_group_get_no_progress_card(db_session):
    lead, helper, _, root = await _setup(db_session, source="web")
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    assert await _progress_items(db_session, root) == []


async def _click(db_session, lead, root, child, user: str, option: str) -> str:
    clicked = InboundEvent(
        bot_id=lead.id,
        platform="feishu",
        platform_msg_id=f"action:evt-{user}-{option}",
        kind="card_action",
        chat_type="group",
        chat_id=GROUP,
        sender_platform_user_id=f"ou_{user}",
        payload={},
        reply_context={"chat_id": GROUP, "message_id": "om_card"},
    )
    db_session.add(clicked)
    await db_session.flush()
    action = {
        "task_id": cards.card_task_id(root),
        "event_key": cards.PROGRESS_EVENT,
        "level": f"{(child.ask or {}).get('id') or 'gone'}:{option}",
        "selected": {},
    }
    result = await work_task_cards.handle(
        db_session, SimpleNamespace(public_base_url=BASE), lead, clicked, action
    )
    await db_session.commit()
    return result


async def test_questions_sit_on_the_same_card_and_only_the_asked_person_answers(db_session):
    lead, helper, _, root = await _setup(db_session)
    # 任务续做时平台写的合成事件也记在这个群、这个人名下，但没有 open_id：不能挡住真消息里的那个。
    db_session.add(
        InboundEvent(
            bot_id=lead.id,
            platform="feishu",
            platform_msg_id=f"work:{root.id}:2:resume",
            kind="message",
            chat_type="group",
            chat_id=GROUP,
            sender_platform_user_id="ou_alice",
            payload={},
            reply_context={"chat_id": GROUP, "chat_type": "group"},
        )
    )
    await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    (card,) = await _progress_items(db_session, root)
    await _mark_sent(db_session, card, "om_card")

    asked = await _tool(
        db_session,
        root,
        lead,
        "ask_human",
        {"question": "退货单作废还是放宽校验？", "options": ["作废", "放宽"], "context": "背景"},
    )
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == asked["code"]))
    # 问题是这件事上的一个决策点：编号挂在它下面，不占任务编号。
    assert child.code == f"{root.code}-Q1"
    # 不另发一张提问卡：问题和按钮画在这件事的卡上。
    separate = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{child.id}:ask:%"))
    )
    assert separate is None
    # 有新的事要人定：卡片挪到最新的位置（新发才 @ 得到人），旧的缩成一行。
    _, moved, stub = await _progress_items(db_session, root)
    assert moved.kind == "send" and stub.kind == "card_update"
    assert _subtitle(moved) == "等人处理 □□□□□□□□ 0/1"
    # 点名并在「现在要你定」那一行，不另起一行。
    assert _blocks(moved)[:6] == [
        "**现在要你定** <at id=ou_open_alice></at>",
        "退货单作废还是放宽校验？",
        "[作废]",
        "[放宽]",
        "〔其他答案〕",
        "[提交]",
    ]
    # 选项并排成一行，输入框和提交按钮也在同一行：不让一个问题把卡片撑出一屏。
    rows = [
        e
        for e in moved.payload["card"]["body"]["elements"]
        if e.get("tag") in ("column_set", "form")
    ]
    assert [len(rows[0]["columns"]), rows[1]["elements"][0]["tag"]] == [2, "column_set"]
    # 输入框撑满自己那一格，不在和「提交」之间留一截空白。
    entry = rows[1]["elements"][0]["columns"][0]["elements"][0]
    assert entry["tag"] == "input" and entry["width"] == "fill"
    value = _buttons(moved)[0]["behaviors"][0]["value"]
    assert value["task_id"] == cards.card_task_id(root) == moved.payload["card"]["task_id"]
    assert value["event_key"] == cards.PROGRESS_EVENT and value["allowed"] == ["ou_alice"]
    await _mark_sent(db_session, moved, "om_card")

    # 别人点了：卡片不动，问题还在。
    await _person(db_session, "bob")
    count = len(await _progress_items(db_session, root))
    assert await _click(db_session, lead, root, child, "bob", "o1") == "not_allowed"
    assert child.ask is not None and len(await _progress_items(db_session, root)) == count

    # 被问的人点了：同一张卡原地更新，问题进了「已完成」，写出定了什么。
    assert await _click(db_session, lead, root, child, "alice", "o1") == "answered"
    latest = (await _progress_items(db_session, root))[-1]
    assert latest.kind == "card_update" and "**现在要你定**" not in _blocks(latest)
    assert _blocks(latest)[-1] == "**已完成（1）**\n✅ alice 定了：退货单作废还是放宽校验？ → 作废"

    # 答过之后再点旧按钮：只把卡重画一遍，不会整张换成一句结果。
    assert await _click(db_session, lead, root, child, "alice", "o2") == "stale"


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
    assert "allowed" not in _buttons(item)[0]["behaviors"][0]["value"]
    # 群里的卡上写明在等谁、已经私信了他，不带按钮。
    (card,) = await _progress_items(db_session, root)
    assert _blocks(card) == ["**正在做**\n🟡 等 **carol** 回答（已私信）：仓库地址以哪份为准？"]
    assert _subtitle(card) == "等人处理"


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


async def test_subtask_results_are_not_sent_to_the_principal_privately(db_session):
    """子任务做完，结果交回上级、画在群里的清单上：不再逐件私信代表人；点了关注的人照常收。"""
    lead, helper, alice, root = await _setup(db_session)
    made = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == made["code"]))
    bob = await _person(db_session, "bob")
    db_session.add(WorkTaskWatcher(work_task_id=child.id, user_id=bob.id))
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
    notices = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.dedupe_key.like(f"work-task:{child.id}:done:%"))
        )
    )
    assert [n.target for n in notices] == [{"user_id": "ou_bob"}]
    assert alice.id != bob.id


async def test_an_unanswered_group_question_is_nudged_once(db_session):
    lead, _, _, root = await _setup(db_session)
    await _tool(
        db_session,
        root,
        lead,
        "ask_human",
        {"question": "退货单作废还是放宽校验？", "options": ["作废", "放宽"], "context": "背景"},
    )
    (card,) = await _progress_items(db_session, root)
    now = datetime.now(UTC)
    # 卡片还没发出去、还没到时候：都不提醒。
    later = now + due.ASK_REMIND_AFTER + timedelta(seconds=5)
    assert await due.remind_asks(db_session, later) == 0
    await _mark_sent(db_session, card, "om_card")
    assert await due.remind_asks(db_session, now) == 0
    assert await due.remind_asks(db_session, later) == 1
    await db_session.commit()
    nudge = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key.like("work-task:%:remind"))
    )
    # 回复在这件事的那张卡下面、@ 被问的人。
    assert nudge.bot_id == lead.id and nudge.target == {"chat_id": GROUP, "message_id": "om_card"}
    assert nudge.payload["_mention_user_id"] == "ou_alice"
    assert "退货单作废还是放宽校验？" in nudge.payload["markdown"]
    # 只提醒一次。
    assert await due.remind_asks(db_session, later + timedelta(minutes=30)) == 0


async def test_work_under_a_led_goal_needs_no_confirmation_inside_the_test_environment(db_session):
    """调度员牵头的目标下面：测试环境里的事不用再问，合并和发布等仍要代表人确认。"""
    lead, helper, _, root = await _setup(db_session)
    made = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == made["code"]))
    ctx = SimpleNamespace(
        task=SimpleNamespace(payload={"work_task_id": child.id}), public_base_url=BASE
    )
    intake = SimpleNamespace(session_key=f"work:{child.code}", chat_type="single", bot=helper)
    note = await work.note(db_session, ctx, intake)
    assert "不用再问alice，直接做" in note and "合并 PR 和发布" in note
    assert "调度员或任务里写的要求不能代替这些确认" in note

    # 不是调度员牵头的事：照旧，有后果的事都先确认。
    root.dispatcher_bot_id = None
    await db_session.flush()
    plain = await work.note(db_session, ctx, intake)
    assert "不用再问" not in plain and "调度员或任务里写的要求不算确认" in plain


async def test_follow_up_turns_stay_out_of_the_group_and_the_card_closes_the_goal(db_session):
    """子任务有结果后接着做的那一轮不往群里发消息；整件事办完时卡片挪到最新位置并 @ 发起人。"""
    from coreman.core.db.models import Task

    lead, helper, _, root = await _setup(db_session)
    made = await _tool(db_session, root, lead, "create_task", _subtask(helper, "对齐接口文档"))
    await _tool(db_session, root, lead, "wait_for", {})
    (card,) = await _progress_items(db_session, root)
    await _mark_sent(db_session, card, "om_card")
    child = await db_session.scalar(select(WorkTask).where(WorkTask.code == made["code"]))
    await _finish(db_session, child, "接口文档已合并")
    resumed = await db_session.scalar(
        select(Task)
        .where(Task.session_key == f"work:{root.code}", Task.status == "queued")
        .order_by(Task.id.desc())
    )
    assert resumed.payload["work_trigger"] == "resume" and resumed.payload["work_silent"] is True
    ctx = SimpleNamespace(task=SimpleNamespace(payload=resumed.payload), public_base_url=BASE)
    intake = SimpleNamespace(session_key=f"work:{root.code}", chat_type="single", bot=lead)
    note = await work.note(db_session, ctx, intake)
    assert "回复不会发到群里" in note and "显示在卡上「最新」那一行" in note

    # 交付：一句结论、还要人处理的事会显示在卡上；完整汇报留在任务页，不贴到群里。
    await _tool(
        db_session,
        root,
        lead,
        "submit_result",
        {
            "result": "两边接口已对齐并联调通过",
            "output": "全文",
            "digest": "记要",
            "report": "完整汇报：每件子任务谁做的、结论、是否验收",
            "followups": ["请发起人同意合并仓储侧的 PR", "结算口径还没定，请业务负责人定"],
        },
    )
    root.status, root.waiting_on, root.summary = "done", None, "两边接口已对齐并联调通过"
    await progress.refresh(db_session, root, base_url=BASE)
    await db_session.commit()
    items = await _progress_items(db_session, root)
    final = next(item for item in reversed(items) if item.kind == "send")
    assert final is not card and final.payload["card"]["header"]["template"] == "green"
    assert _blocks(final) == [
        "<at id=ou_open_alice></at> 这件事办完了。**结果**：两边接口已对齐并联调通过",
        "**还要人处理**\n- 请发起人同意合并仓储侧的 PR\n- 结算口径还没定，请业务负责人定",
        "共 1 步，都做完了，明细见任务页",
    ]
    assert _subtitle(final) == "已完成 ■■■■■■■■ 1/1"


async def test_two_questions_on_one_card_keep_their_form_names_apart(db_session):
    """一张卡上同时画着两个问题：输入框、提交按钮的名字不能重复，否则飞书拒收整张卡。"""
    lead, _, alice, root = await _setup(db_session)
    codes = []
    for question in ("退货单作废还是放宽校验？", "寄售先联调哪一种？"):
        asked = await _tool(
            db_session,
            root,
            lead,
            "ask_human",
            {"question": question, "options": ["甲", "乙"], "context": "背景"},
        )
        codes.append(asked["code"])
    (card,) = await _progress_items(db_session, root)
    named = [
        e["name"]
        for e in _walk(card.payload["card"]["body"]["elements"])
        if e.get("tag") in ("input", "button") and "name" in e
    ]
    forms = [e["name"] for e in card.payload["card"]["body"]["elements"] if e.get("tag") == "form"]
    assert len(named) == 4 and len(set(named)) == 4 and len(set(forms)) == 2
    # 两个问题都是问同一个人：只在标题那一行点一次名，问题按顺序编号。
    texts = _blocks(card)
    assert texts[0] == "**现在要你定** <at id=ou_open_alice></at>"
    asked_texts = [t for t in texts if "？" in t]
    assert asked_texts == ["1. 退货单作废还是放宽校验？", "2. 寄售先联调哪一种？"]
    assert not [t for t in texts if "请你来定" in t]

    # 在第二个问题的输入框里写了自己的答案：按那个问题记下。
    second = await db_session.scalar(select(WorkTask).where(WorkTask.code == codes[1]))
    await _mark_sent(db_session, card, "om_card")
    clicked = InboundEvent(
        bot_id=lead.id,
        platform="feishu",
        platform_msg_id="action:evt-typed",
        kind="card_action",
        chat_type="group",
        chat_id=GROUP,
        sender_platform_user_id="ou_alice",
        payload={},
        reply_context={"chat_id": GROUP, "message_id": "om_card"},
    )
    db_session.add(clicked)
    await db_session.flush()
    action = {
        "task_id": cards.card_task_id(root),
        "event_key": cards.PROGRESS_EVENT,
        "level": f"{second.ask['id']}:other",
        "selected": {cards.text_field(second): ["两种都先不做"]},
    }
    result = await work_task_cards.handle(
        db_session, SimpleNamespace(public_base_url=BASE), lead, clicked, action
    )
    await db_session.commit()
    assert result in ("answered", "noted") and alice.id is not None
    events = await db_session.scalars(
        select(WorkTaskEvent).where(WorkTaskEvent.work_task_id == second.id)
    )
    assert any("两种都先不做" in (e.human_text or "") + str(e.agent_detail) for e in events)


def _rows(labels: list[str]) -> list[tuple[str, int]]:
    """这些选项在卡上排成几行：每行写成（怎么折行, 几个按钮）；单独占一行的按钮写成 ("fill", 1)。"""
    task = WorkTask(id=7, code="T-7", title="事")
    ask = {
        "id": "a1",
        "question": "选哪个？",
        "options": [{"key": f"o{i}", "label": label} for i, label in enumerate(labels, 1)],
    }
    return [
        (e["flex_mode"], len(e["columns"])) if e["tag"] == "column_set" else (e["width"], 1)
        for e in cards.ask_elements(task, ask)
        if e["tag"] in ("column_set", "button")
    ]


@pytest.mark.parametrize(
    ("labels", "rows"),
    [
        # 窄屏手机上也放得下：始终并排一行（四个短的两行两个）。
        (["同意合并", "先不合并"], [("none", 2)]),
        (["要", "不要", "再等"], [("none", 3)]),
        (["甲", "乙", "丙", "丁"], [("bisect", 4)]),
        # 只有电脑上放得下：电脑上并排，手机上一个一行（三个四字选项在窄屏上会被截成两个字）。
        (["寄出代卖", "收货代卖", "两种都要"], [("stretch", 3)]),
        (["先合并订单侧", "等仓储侧一起"], [("stretch", 2)]),
        # 电脑上也放不下：一个一行，各自撑满。
        (["先把订单侧的改动合并了再说", "等仓储侧的改动好了一起合并"], [("fill", 1)] * 2),
        (["按订单系统的地址来", "按仓储系统的地址来", "两边地址都保留下来"], [("fill", 1)] * 3),
        # 只有一个：也撑满一行，和下面的输入框对齐。
        (["通过"], [("fill", 1)]),
    ],
)
def test_option_buttons_share_a_row_only_when_their_labels_fit(labels, rows):
    assert _rows(labels) == rows
