"""运行中卡片「看看进度」：引用那张卡回一条实时进度，不改卡片。"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import liveness
from coreman.core.db.models import Bot, FeishuDelivery, OutboxItem, Task
from coreman.core.i18n.messages import msg
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _running(session: AsyncSession, bot: Bot) -> Task:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", session_key="oc1", payload={})
    )
    assert task is not None
    await instances.register(
        session, instance_id="worker-chat", service="worker", version="dev", capacity=8
    )
    assert await tasks.claim(session, lane="normal", instance_id="worker-chat")
    await tasks.start(session, task.id)
    started = datetime.now(UTC) - timedelta(minutes=3)
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s",
        reply_context={"chat_id": "oc1", "message_id": "om_q"},
        lease_generation=1,
        running_since=started,
    )
    activity, _ = liveness.tool_activity("Bash", "等 CI 跑完", "gh run watch 1", started)
    await streams.update(
        session,
        task.id,
        thinking_md="🔧 **运行命令**：等 CI 跑完",
        pending_text="正在等 CI",
        activity=activity,
    )
    session.add(FeishuDelivery(task_id=task.id, card_id="card1", message_id="om_card"))
    await session.commit()
    return task


async def _click(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, target: Task, message_id: str
) -> OutboxItem | None:
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="card_action",
        chat_type="group",
        chat_id="oc1",
        sender={"platform_user_id": "someone"},
        message_id=f"a-{uuid.uuid4()}",
        card_action={
            "task_id": liveness.live_task_id(target.id),
            "card_type": "form",
            "event_key": liveness.LIVE_PROGRESS,
        },
        reply_context={"chat_id": "oc1", "message_id": message_id},
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
        select(OutboxItem).where(OutboxItem.kind == "send").order_by(OutboxItem.id.desc())
    )


async def test_click_replies_with_live_progress(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    target = await _running(db_session, bot)
    item = await _click(db_engine, db_session, bot, target, "om_card")
    assert item is not None
    assert item.target == {"chat_id": "oc1", "message_id": "om_card"}
    assert item.payload["_reply"] is True
    text = item.payload["text"]
    assert text.startswith("⏳ 进行中") and "等 CI 跑完" in text
    assert "已经处理了 3 分" in text and "做了 1 步" in text and "正在等 CI" in text
    # 每次点击各回一条。
    again = await _click(db_engine, db_session, bot, target, "om_card")
    assert again is not None and again.id != item.id


async def test_click_on_another_message_is_ignored(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    target = await _running(db_session, bot)
    assert await _click(db_engine, db_session, bot, target, "om_other") is None


async def test_click_after_finish_says_done(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    target = await _running(db_session, bot)
    await streams.complete(db_session, target.id, final_text="好了")
    await db_session.commit()
    item = await _click(db_engine, db_session, bot, target, "om_card")
    assert item is not None and item.payload["text"] == msg("live_progress_done")
