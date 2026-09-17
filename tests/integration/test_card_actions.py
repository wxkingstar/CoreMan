"""企微模板卡片回调的语义处理（投票卡片、限流切换卡片）。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import interactions
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, Task
from coreman.core.i18n.messages import msg
from coreman.core.wecom.cards import (
    answered_card,
    expired_card,
    notice_card,
    question_brief,
    waiting_card,
)
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.worker_helpers import build_ctx, seed_bot

Q1 = {
    "question": "用哪个？",
    "header": "h",
    "options": [{"label": "A", "description": "a"}, {"label": "B", "description": ""}],
    "multiSelect": True,
}
Q2 = {
    "question": "第二题",
    "header": "",
    "options": [{"label": "X", "description": ""}],
    "multiSelect": False,
}
PREFIX = "choice@sales_bot@zs@1700000000abcdef01"


async def _state(session: AsyncSession, bot: Bot, *, questions, index=0, answers=None, user="zs"):  # type: ignore[no-untyped-def]
    st = await interactions.open_state(
        session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, user),
        state={
            "questions": questions,
            "answers": answers or [],
            "current_index": index,
            "waiting_for_text": False,
            "waiting_since": None,
            "context": {
                "platform_user_id": user,
                "chat_id": user,
                "chat_type": "single",
                "relay_session_id": str(uuid.uuid4()),
            },
        },
        task_id_prefix=PREFIX,
    )
    await session.commit()
    return st


async def _card_task(
    session: AsyncSession,
    bot: Bot,
    *,
    task_id: str,
    selected: dict[str, list[str]],
    user: str = "zs",
):  # type: ignore[no-untyped-def]
    ev = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=f"e-{uuid.uuid4()}",
        kind="card_action",
        chat_type="single",
        chat_id=user,
        sender_platform_user_id=user,
        payload={},
        reply_context={
            "gateway_instance": "gw",
            "req_id": f"r-{task_id}",
            "chat_type": "single",
            "chat_id": user,
        },
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={
                "card_action": {
                    "task_id": task_id,
                    "card_type": "vote_interaction",
                    "event_key": "submit_choice",
                    "selected": selected,
                },
                "bot_key": bot.bot_key,
                "platform_user_id": user,
                "chat_type": "single",
                "chat_id": user,
            },
            session_key=user,
            inbound_event_id=ev.id,
            dedupe_key=f"inbound:{ev.id}",
        ),
    )
    await session.commit()
    assert t
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    # 快车道上可能还排着上一次点击排出来的 relay_switch 任务；一直认领到自己这条为止。
    claimed = await tasks.claim(session, lane="fast", instance_id="worker-test")
    while claimed is not None and claimed.id != t.id:
        claimed = await tasks.claim(session, lane="fast", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == t.id
    return claimed, ev


async def _outbox(session: AsyncSession) -> list[OutboxItem]:
    return list((await session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all())


async def test_answer_first_question_then_next(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    st = await _state(db_session, bot, questions=[Q1, Q2])
    t, ev = await _card_task(
        db_session, bot, task_id=f"{PREFIX}@0", selected={"choice_answer": ["opt_1", "opt_0"]}
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    items = await _outbox(db_session)
    assert [i.kind for i in items] == ["card_update", "send", "send"]
    assert (
        items[0].target == {"req_id": f"r-{PREFIX}@0", "task_id": f"{PREFIX}@0"}
        and items[0].dedupe_key == f"{ev.id}:card_update"
    )
    assert items[0].payload["card"] == answered_card(
        Q1, index=0, total=2, task_id=f"{PREFIX}@0", answer="B, A", is_last=False, icon_url=""
    )
    assert items[1].payload == {"markdown": question_brief(Q2, index=1, total=2)}
    assert items[2].payload["card"]["task_id"] == f"{PREFIX}@1"
    await db_session.refresh(st)
    assert st.state["answers"] == ["B, A"] and st.state["current_index"] == 1
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded" and row.result == {"card": "answered"}


async def test_last_answer_submits_and_other_waits(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    st = await _state(db_session, bot, questions=[Q1])
    t, _ = await _card_task(
        db_session, bot, task_id=f"{PREFIX}@0", selected={"choice_answer": ["opt_other"]}
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    items = await _outbox(db_session)
    assert len(items) == 1 and items[0].payload["card"] == waiting_card(
        Q1, task_id=f"{PREFIX}@0", icon_url=""
    )
    await db_session.refresh(st)
    assert st.state["waiting_for_text"] is True and st.state["waiting_since"]
    t2, _ = await _card_task(db_session, bot, task_id=f"{PREFIX}@0", selected={"choice_answer": []})
    await CardActionHandler().run(build_ctx(db_engine, t2))
    items = await _outbox(db_session)
    assert items[1].payload["card"]["sub_title_text"] == (
        msg("card_answered_sub", answer=msg("card_no_selection")) + "\n" + msg("choice_generating")
    )
    assert items[2].payload == {"markdown": msg("choice_generating")}
    submit = (
        await db_session.execute(select(Task).where(Task.kind == "choice_submit"))
    ).scalar_one()
    assert submit.dedupe_key == f"choice_submit:{st.id}" and submit.payload["state_id"] == str(
        st.id
    )
    await db_session.refresh(st)
    assert st.state["waiting_for_text"] is False and st.status == "open"


async def test_ignored_and_expired_cases(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _r, _c = await seed_bot(db_session)
    await _state(db_session, bot, questions=[Q1, Q2], index=1)
    # 旧卡片
    t, _ = await _card_task(
        db_session, bot, task_id=f"{PREFIX}@0", selected={"choice_answer": ["opt_0"]}
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    assert await _outbox(db_session) == []
    # 非发起人
    t2, _ = await _card_task(
        db_session, bot, task_id=f"{PREFIX}@1", selected={"choice_answer": ["opt_0"]}, user="ls"
    )
    await CardActionHandler().run(build_ctx(db_engine, t2))
    assert await _outbox(db_session) == []
    # 不存在
    t3, _ = await _card_task(
        db_session,
        bot,
        task_id="choice@sales_bot@zs@9999@0",
        selected={"choice_answer": ["opt_0"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t3))
    items = await _outbox(db_session)
    assert items[0].payload["card"] == expired_card("choice@sales_bot@zs@9999@0", icon_url="")
    t4, _ = await _card_task(db_session, bot, task_id="garbage", selected={})
    await CardActionHandler().run(build_ctx(db_engine, t4))
    row = await tasks.get(db_session, t4.id)
    assert row and row.status == "succeeded" and row.result == {"card": "unknown"}
    assert len(await _outbox(db_session)) == 1


async def test_ratelimit_switch_branches(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, relay, _c = await seed_bot(db_session)
    now = datetime.now(UTC)
    rl_prefix = "ratelimit_switch@sales_bot@zs@1700000000"
    state = {
        "idx": 0,
        "platform_user_id": "zs",
        "user_id": None,
        "current_relay_id": str(relay.id),
        "current_name": "r1",
        "target_relay_id": str(uuid.uuid4()),
        "target_name": "r2",
        "target_pct_7d": 12.0,
        "chat_id": "zs",
        "chat_type": "single",
    }
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state=state,
        task_id_prefix=rl_prefix,
        expires_at=now + timedelta(minutes=30),
    )
    await db_session.commit()
    t, _ = await _card_task(
        db_session, bot, task_id=f"{rl_prefix}@0", selected={"ratelimit_switch_choice": []}
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    items = await _outbox(db_session)
    assert items[-1].payload["card"] == notice_card(
        f"{rl_prefix}@0",
        title=msg("rl_no_option_title"),
        desc=msg("rl_no_option_desc"),
        icon_url="",
    )
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}@0",
        selected={"ratelimit_switch_choice": ["opt_switch"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    items = await _outbox(db_session)
    assert items[-1].payload["card"]["main_title"]["title"] == msg("rl_switching_title")
    assert items[-1].payload["card"]["sub_title_text"] == msg(
        "rl_switching_desc", current="r1", target="r2"
    )
    sw = (await db_session.execute(select(Task).where(Task.kind == "relay_switch"))).scalar_one()
    assert (
        sw.lane == "fast"
        and sw.payload["target_relay_server_id"] == state["target_relay_id"]
        and sw.dedupe_key == f"relay_switch:{st.id}"
    )
    await db_session.refresh(st)
    assert st.status == "submitted"
    # 再点同一张卡：已提交 → 过期提示；有更新的卡时 → 已被替代。
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}@0",
        selected={"ratelimit_switch_choice": ["opt_wait"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    assert (await _outbox(db_session))[-1].payload["card"]["main_title"]["title"] == msg(
        "rl_expired_title"
    )
    newer = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={**state, "idx": 1},
        task_id_prefix=rl_prefix + "1",
        expires_at=now + timedelta(minutes=30),
    )
    await db_session.commit()
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}@0",
        selected={"ratelimit_switch_choice": ["opt_wait"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    assert (await _outbox(db_session))[-1].payload["card"]["main_title"]["title"] == msg(
        "rl_replaced_title"
    )
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}1@1",
        selected={"ratelimit_switch_choice": ["opt_wait"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    last = (await _outbox(db_session))[-1].payload["card"]
    assert last["main_title"]["title"] == msg("rl_wait_title") and last["sub_title_text"] == msg(
        "rl_wait_desc", current="r1"
    )
    await db_session.refresh(newer)
    assert newer.status == "submitted"
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}1@1",
        selected={"ratelimit_switch_choice": ["opt_wait"]},
        user="ls",
    )
    before = len(await _outbox(db_session))
    await CardActionHandler().run(build_ctx(db_engine, t))
    assert len(await _outbox(db_session)) == before  # 非发起人静默


async def test_ratelimit_wait_loses_the_race_with_a_concurrent_switch(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次点击都过了 `_still_open`，先落地的那次把状态收走：后到的「等待」必须回过期卡。

    `set_status` 是 `UPDATE … WHERE status='open'`，抢输的那次返回 False。不看返回值就会
    在切换任务已经排好的情况下回一张「已选择继续等待」，用户以为没切、实际正在切。
    """
    bot, relay, _c = await seed_bot(db_session)
    rl_prefix = "ratelimit_switch@sales_bot@zs@1700000002"
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "idx": 0,
            "platform_user_id": "zs",
            "user_id": None,
            "current_relay_id": str(relay.id),
            "current_name": "r1",
            "target_relay_id": str(uuid.uuid4()),
            "target_name": "r2",
            "target_pct_7d": 12.0,
            "chat_id": "zs",
            "chat_type": "single",
        },
        task_id_prefix=rl_prefix,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await db_session.commit()

    async def _lost(*_a: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(interactions, "set_status", _lost)
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}@0",
        selected={"ratelimit_switch_choice": ["opt_wait"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    assert (await _outbox(db_session))[-1].payload["card"] == notice_card(
        f"{rl_prefix}@0",
        title=msg("rl_expired_title"),
        desc=msg("rl_expired_desc"),
        icon_url="",
    )
    row = await tasks.get(db_session, t.id)
    assert row and row.result == {"card": "expired"}
    await db_session.refresh(st)
    assert st.status == "open"  # 状态没被这一路动过


async def test_ratelimit_unknown_option_keeps_card_open(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """选项 id 不认识（企微改版、卡片被篡改）：回一张告知卡，但不消耗这张待答状态。"""
    bot, relay, _c = await seed_bot(db_session)
    rl_prefix = "ratelimit_switch@sales_bot@zs@1700000001"
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "idx": 0,
            "platform_user_id": "zs",
            "user_id": None,
            "current_relay_id": str(relay.id),
            "current_name": "r1",
            "target_relay_id": str(uuid.uuid4()),
            "target_name": "r2",
            "target_pct_7d": 12.0,
            "chat_id": "zs",
            "chat_type": "single",
        },
        task_id_prefix=rl_prefix,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await db_session.commit()
    t, _ = await _card_task(
        db_session,
        bot,
        task_id=f"{rl_prefix}@0",
        selected={"ratelimit_switch_choice": ["opt_bogus"]},
    )
    await CardActionHandler().run(build_ctx(db_engine, t))
    items = await _outbox(db_session)
    assert items[-1].payload["card"] == notice_card(
        f"{rl_prefix}@0",
        title=msg("rl_unknown_title"),
        desc=msg("rl_unknown_desc", option="opt_bogus"),
        icon_url="",
    )
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded" and row.result == {"card": "unknown_option"}
    await db_session.refresh(st)
    assert st.status == "open"


@pytest.mark.parametrize("bound", [True, False])
async def test_feishu_callback_requires_actual_sent_card(db_engine, db_session, bound):
    from coreman.core.bus import outbox

    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    state = await _state(db_session, bot, questions=[Q1, Q2])
    task, event = await _card_task(
        db_session, bot, task_id=f"{PREFIX}@0", selected={"choice_answer": ["opt_0"]}
    )
    event.platform = "feishu"
    event.reply_context = {**event.reply_context, "message_id": "om_card"}
    if bound:
        item = await outbox.add(
            db_session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="initial",
            target={"chat_id": "zs"},
            payload={"card": {"task_id": f"{PREFIX}@0"}, "_feishu_message_id": "om_card"},
        )
        await outbox.mark_sent(db_session, item.id)
    await db_session.commit()
    await CardActionHandler().run(build_ctx(db_engine, task))
    await db_session.refresh(state)
    assert state.state["current_index"] == (1 if bound else 0)
    items = await _outbox(db_session)
    updates = [item for item in items if item.kind == "card_update"]
    assert len(updates) == int(bound)
    if bound:
        assert updates[0].target["message_id"] == "om_card"
