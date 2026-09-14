import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.chat import sessions
from coreman.core.db.models import Bot, InboundEvent, TaskStream
from coreman.core.i18n.messages import msg
from coreman.runtime.bus import instances, tasks
from coreman.runtime.bus.tasks import NewTask
from coreman.runtime.worker.commands import CommandHandler
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _command_task(db_session: AsyncSession, bot: Bot, command: str, session_key: str = "zs"):  # type: ignore[no-untyped-def]
    ev = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=f"m-{uuid.uuid4()}",
        kind="message",
        chat_type="single",
        chat_id=session_key,
        sender_platform_user_id=session_key,
        payload={"text": command},
        reply_context={"gateway_instance": "gw", "req_id": f"r-{command}"},
    )
    db_session.add(ev)
    await db_session.flush()
    t = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="command",
            lane="fast",
            payload={
                "command": command,
                "bot_key": bot.bot_key,
                "platform_user_id": session_key,
            },
            session_key=session_key,
            inbound_event_id=ev.id,
        ),
    )
    await db_session.commit()
    assert t
    return t


async def test_reset_and_stop(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _relay, _cipher = await seed_bot(db_session)
    await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="zs",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    running = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="zs")
    )
    assert running
    await db_session.commit()
    # tasks.claimed_by 有外键，认领之前实例必须先登记（与 tests/integration/test_bus_tasks.py 同）。
    await instances.register(
        db_session, instance_id="w", service="worker", version="dev", capacity=1
    )
    assert await tasks.claim(db_session, lane="normal", instance_id="w") is not None
    await tasks.start(db_session, running.id)
    await db_session.commit()
    reset = await _command_task(db_session, bot, "reset")
    await CommandHandler().run(build_ctx(db_engine, reset))
    assert await sessions.list_for_bot(db_session, bot.id) == []
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == reset.id))
    ).scalar_one()
    assert (
        stream.is_complete
        and stream.final_text == msg("session_reset_ok")
        and stream.reply_context["req_id"] == "r-reset"
    )
    stop = await _command_task(db_session, bot, "stop")
    await CommandHandler().run(build_ctx(db_engine, stop))
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == stop.id))
    ).scalar_one()
    assert stream.final_text == msg("stopped")
    row = await tasks.get(db_session, running.id)
    assert row and row.cancel_reason == "user_stop"
    stop2 = await _command_task(db_session, bot, "stop", session_key="other")
    await CommandHandler().run(build_ctx(db_engine, stop2))
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == stop2.id))
    ).scalar_one()
    assert stream.final_text == msg("nothing_running")
    done = await tasks.get(db_session, stop2.id)
    assert done and done.status == "succeeded"


async def test_commands_clear_pending_states_and_respect_announcements(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.chat import interactions
    from coreman.core.db.models import Announcement, InteractionState

    bot, _relay, _cipher = await seed_bot(db_session)
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={},
    )
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="session_switch",
        scope_key=interactions.session_scope(bot.id, "zs"),
        state={},
    )
    await db_session.commit()
    reset = await _command_task(db_session, bot, "reset")  # payload 里带 platform_user_id="zs"
    await CommandHandler().run(build_ctx(db_engine, reset))
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    db_session.add(Announcement(scope="global", content="全站维护"))
    await db_session.commit()
    stop = await _command_task(db_session, bot, "stop")
    await CommandHandler().run(build_ctx(db_engine, stop))
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == stop.id))
    ).scalar_one()
    assert stream.final_text == "全站维护"


async def test_reset_resolves_open_userid_before_clearing_states(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """快车道 reset：待答状态按解析后的明文 userid 建，用密文 id 发命令也要清得掉。"""
    from coreman.core.chat import interactions
    from coreman.core.db.models import InteractionState, User, UserIdentity

    wo = "wo" + "A" * 40
    bot, _relay, _cipher = await seed_bot(db_session)
    zhangsan = User(login_name="zhangsan", display_name="张三")
    db_session.add(zhangsan)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=zhangsan.id, platform="wecom", platform_user_id="zs", open_id=wo)
    )
    for kind in ("choice", "relay_switch"):
        await interactions.open_state(
            db_session,
            bot_id=bot.id,
            kind=kind,
            scope_key=interactions.choice_scope(bot.id, "zs"),
            state={},
        )
    await db_session.commit()
    # 网关给单聊命令写的 session_key/platform_user_id 都是这串密文 id。
    reset = await _command_task(db_session, bot, "reset", session_key=wo)
    await CommandHandler().run(build_ctx(db_engine, reset))
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    stream = (
        await db_session.execute(select(TaskStream).where(TaskStream.task_id == reset.id))
    ).scalar_one()
    assert stream.final_text == msg("session_reset_ok")
    # 库里没有这个人：发言者 id 保持密文原样，按原样建的状态同样清得掉。
    stranger = "wo" + "B" * 40
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, stranger),
        state={},
    )
    await db_session.commit()
    reset2 = await _command_task(db_session, bot, "reset", session_key=stranger)
    await CommandHandler().run(build_ctx(db_engine, reset2))
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
