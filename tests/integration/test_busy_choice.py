"""上一条还在处理时又发了一条：新消息排队，卡片让本人选停下上一条还是等它做完。"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.chat import busy_choice
from coreman.core.db.models import Bot, OutboxItem, Task, TaskStream
from coreman.core.i18n.messages import msg
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _say(session: AsyncSession, bot: Bot, text: str, *, user: str = "zs") -> Task:
    mid = f"m-{uuid.uuid4()}"
    message = InboundMessage(
        platform=bot.platform,
        bot_id=bot.id,
        kind="message",
        chat_type="single",
        chat_id=user,
        sender={"platform_user_id": user},
        message_id=mid,
        parts=[{"type": "text", "text": text}],
        reply_context={"chat_id": user, "chat_type": "single", "message_id": mid},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    assert task is not None
    return task


async def _click(
    engine: AsyncEngine,
    session: AsyncSession,
    bot: Bot,
    target: Task,
    key: str,
    *,
    user="zs",
    message_id: str = "",
) -> OutboxItem | None:
    message = InboundMessage(
        platform=bot.platform,
        bot_id=bot.id,
        kind="card_action",
        chat_type="single",
        chat_id="zs",
        sender={"platform_user_id": user},
        message_id=f"a-{uuid.uuid4()}",
        card_action={
            "task_id": busy_choice.card_task_id(target.id),
            "card_type": "button_interaction",
            "event_key": key,
        },
        reply_context={
            "chat_id": "zs",
            "chat_type": "single",
            "req_id": "r1",
            **({"message_id": message_id} if message_id else {}),
        },
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    assert task is not None
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="fast", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == task.id
    await CardActionHandler().run(build_ctx(engine, claimed))
    return await session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update").order_by(OutboxItem.id.desc())
    )


async def _refresh(session: AsyncSession, task: Task) -> Task:
    row = await session.get(Task, task.id, populate_existing=True)
    assert row is not None
    return row


async def test_a_second_message_waits_and_offers_a_choice(db_session: AsyncSession) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    assert not first.payload.get("serialize_session")
    assert await db_session.scalar(select(OutboxItem)) is None

    second = await _say(db_session, bot, "改成导出本周")
    assert second.payload["serialize_session"] is True
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert card.target == {"chat_id": "zs"}
    assert card.payload["card"] == busy_choice.offer_card("wecom", second.id)
    assert card.payload["card"]["main_title"]["title"] == msg("busy_title")

    # 连着再发：照样排队，但不再刷一张卡。
    third = await _say(db_session, bot, "再加上退货")
    assert third.payload["serialize_session"] is True
    sends = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).all()
    assert len(sends) == 1
    assert (await _refresh(db_session, first)).cancel_requested_at is None


async def test_feishu_card_quotes_the_new_message(db_session: AsyncSession) -> None:
    bot, _r, _c = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert card.payload["_reply"] is True
    assert card.target["message_id"] == second.payload["message"]["message_id"]
    assert card.payload["card"]["task_id"] == busy_choice.card_task_id(second.id)


async def test_stop_cancels_the_running_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    await db_session.execute(update(Task).where(Task.id == first.id).values(status="running"))
    await db_session.commit()
    second = await _say(db_session, bot, "改成导出本周")

    reply = await _click(db_engine, db_session, bot, second, busy_choice.STOP)
    first = await _refresh(db_session, first)
    assert first.cancel_requested_at is not None and first.cancel_reason == "user_stop"
    second = await _refresh(db_session, second)
    assert second.cancel_requested_at is None
    assert second.payload["stopped_previous"] is True
    assert reply is not None
    assert reply.payload["card"]["main_title"]["title"] == msg("busy_stopped_title")


async def test_stopping_a_message_that_never_started_adds_no_note(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    await _click(db_engine, db_session, bot, second, busy_choice.STOP)
    assert (await _refresh(db_session, first)).cancel_requested_at is not None
    assert not (await _refresh(db_session, second)).payload.get("stopped_previous")


async def test_wait_leaves_the_running_message_alone(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    reply = await _click(db_engine, db_session, bot, second, busy_choice.WAIT)
    assert (await _refresh(db_session, first)).cancel_requested_at is None
    assert reply is not None
    assert reply.payload["card"]["main_title"]["title"] == msg("busy_wait_title")


async def test_cancel_drops_the_new_message_only(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    reply = await _click(db_engine, db_session, bot, second, busy_choice.CANCEL)
    assert (await _refresh(db_session, first)).cancel_requested_at is None
    dropped = await _refresh(db_session, second)
    assert (dropped.status, dropped.error_code) == ("cancelled", "user_dropped")
    assert reply is not None
    assert reply.payload["card"]["main_title"]["title"] == msg("busy_dropped_title")


async def test_someone_else_cannot_stop_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    reply = await _click(db_engine, db_session, bot, second, busy_choice.STOP, user="ls")
    assert reply is None
    assert (await _refresh(db_session, first)).cancel_requested_at is None


async def test_a_late_click_only_says_it_has_started(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    await db_session.execute(update(Task).where(Task.id == first.id).values(status="succeeded"))
    await db_session.execute(update(Task).where(Task.id == second.id).values(status="running"))
    await db_session.commit()
    reply = await _click(db_engine, db_session, bot, second, busy_choice.STOP)
    assert reply is not None
    assert reply.payload["card"]["main_title"]["title"] == msg("busy_late_title")


async def test_progress_shows_a_progress_card_and_drops_the_new_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    await db_session.execute(update(Task).where(Task.id == first.id).values(status="running"))
    db_session.add(
        TaskStream(
            task_id=first.id,
            bot_id=bot.id,
            platform="wecom",
            stream_id="s1",
            reply_context={},
            lease_generation=1,
            running_since=datetime.now(UTC) - timedelta(minutes=6, seconds=3),
            thinking_md="🤔 开始\n🔧 **Bash**\n🔧 **Read**",
            pending_text="我先查一下订单表。\n\n正在汇总上周的退货数据",
        )
    )
    await db_session.commit()
    second = await _say(db_session, bot, "进展怎么样了？")

    reply = await _click(db_engine, db_session, bot, second, busy_choice.PROGRESS)
    assert reply is not None
    card = reply.payload["card"]
    # 看完进度这条就放弃：卡上没有按钮，上一条不受影响。
    assert "button_list" not in card
    assert card["main_title"]["title"] == msg("busy_progress_title")
    desc = card["sub_title_text"]
    assert desc.startswith("已处理 6 分 ")
    assert " · 做了 2 步\n" in desc
    assert "最近几步：Bash → Read" in desc
    assert "最近写到：正在汇总上周的退货数据" in desc
    assert desc.endswith(msg("busy_progress_dropped"))
    assert (await _refresh(db_session, first)).cancel_requested_at is None
    dropped = await _refresh(db_session, second)
    assert (dropped.status, dropped.error_code) == ("cancelled", "user_dropped")


async def test_progress_after_the_previous_one_finished_keeps_the_new_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    first = await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "进展怎么样了？")
    await db_session.execute(update(Task).where(Task.id == first.id).values(status="succeeded"))
    await db_session.commit()

    reply = await _click(db_engine, db_session, bot, second, busy_choice.PROGRESS)
    assert reply is not None
    card = reply.payload["card"]
    assert card["main_title"]["title"] == msg("busy_prevdone_title")
    assert "button_list" not in card
    assert (await _refresh(db_session, second)).status == "queued"


def test_feishu_progress_card_shows_steps_and_recent_lines() -> None:
    view = busy_choice.Progress(
        status="running",
        seconds=1368,
        quiet_seconds=451,
        thinking_md="🤔 开始\n🔧 **Bash**\n🔧 **Bash**\n🔧 **Read**\n💭 想一下\n🔧 **Edit**",
        reply_text="# 小结\n- 改好了\n```\n测试在重跑",
        mark="m",
    )
    card = busy_choice.progress_card("feishu", 7, view)
    assert card["header"]["title"]["content"] == msg("busy_progress_title")
    assert card["config"]["summary"]["content"] == "已处理 22 分 48 秒 · 做了 4 步"
    texts = [e["content"] for e in card["body"]["elements"] if e["tag"] == "markdown"]
    assert texts[0] == (
        "**已处理 22 分 48 秒 · 做了 4 步**\n<font color='orange'>最近 7 分 31 秒 没有新动作</font>"
    )
    assert texts[1].endswith("\nBash ×2 → Read → Edit")
    assert texts[2].endswith("\n小结\n改好了\n测试在重跑")
    assert msg("busy_ask") not in "".join(texts)
    assert msg("busy_progress_dropped") in texts[-1]
    assert all(e["tag"] in ("markdown", "hr") for e in card["body"]["elements"])


def test_progress_of_a_message_still_waiting_in_line() -> None:
    text = busy_choice.progress_text(status="queued", seconds=0, thinking_md="", reply_text="")
    assert text == msg("busy_progress_queued")


def test_progress_without_steps_or_text_only_says_how_long() -> None:
    text = busy_choice.progress_text(status="running", seconds=42, thinking_md="", reply_text="")
    assert text == "上一条已经处理了 42 秒。"


def test_feishu_buttons_stack_full_width() -> None:
    elements = busy_choice.offer_card("feishu", 7)["body"]["elements"]
    buttons = [e for e in elements if e["tag"] == "button"]
    assert [b["behaviors"][0]["value"]["event_key"] for b in buttons] == [
        busy_choice.WAIT,
        busy_choice.STOP,
        busy_choice.PROGRESS,
        busy_choice.CANCEL,
    ]
    assert all(b["width"] == "fill" for b in buttons)
    assert all(e["tag"] != "column_set" for e in elements)


async def test_feishu_wait_keeps_a_working_preview(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _r, _c = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    await _say(db_session, bot, "导出上周订单")
    second = await _say(db_session, bot, "改成导出本周")
    # 飞书只认已经发出去的卡：模拟网关发送成功，回填平台消息 id。
    offer = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    offer.status = "sent"
    offer.payload = {**offer.payload, "_feishu_message_id": "om-card"}
    await db_session.commit()
    reply = await _click(db_engine, db_session, bot, second, busy_choice.WAIT, message_id="om-card")
    assert reply is not None
    card = reply.payload["card"]
    assert card["header"]["title"]["content"] == msg("busy_wait_title")
    # 会话列表预览得说上一条还在跑，不然盖掉「生成中」后看着像挂了。
    assert card["config"]["summary"]["content"] == msg("busy_wait_summary")


def test_other_results_keep_the_default_preview() -> None:
    card = busy_choice.result_card("feishu", "busy@1", title="t", desc="d")
    assert "config" not in card
