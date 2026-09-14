import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    Bot,
    BotLease,
    ChatLog,
    ChatSession,
    InboundEvent,
    OutboxItem,
    ProcessInstance,
    Task,
    TaskStream,
    User,
    UserReached,
)


async def _bot(session: AsyncSession, key: str = "b1") -> Bot:
    user = User(login_name=f"creator-{key}", display_name="创建者")
    session.add(user)
    await session.flush()
    bot = Bot(
        bot_key=key,
        platform="wecom",
        name=key,
        created_by=user.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/data/skills/b1",
        credentials_enc="enc:v1:x",
    )
    session.add(bot)
    await session.commit()
    return bot


async def test_tables_exist_with_partial_indexes(db_session: AsyncSession) -> None:
    names = {
        r[0]
        for r in (
            await db_session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
            )
        ).all()
    }
    assert {
        "tasks_claim_idx",
        "tasks_active_session_idx",
        "tasks_heartbeat_idx",
        "task_streams_active_idx",
        "outbox_pending_idx",
        "chat_logs_bot_time_idx",
        "chat_logs_user_time_idx",
        "chat_logs_session_idx",
        "chat_logs_status_idx",
        "bots_team_id_idx",
        "bots_created_by_idx",
        "bot_members_user_id_idx",
    } <= names


async def test_task_defaults_and_status_check(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    task = Task(bot_id=bot.id, kind="chat", session_key="u1", payload={"text": "hi"})
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    assert task.status == "queued" and task.lane == "normal" and task.attempts == 0
    assert task.priority == 0
    assert task.run_after is not None and task.id > 0
    task.status = "bogus"
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_inbound_dedupe_and_stream_cascade(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    ev = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id="m1",
        kind="message",
        chat_type="single",
        chat_id="u1",
        sender_platform_user_id="u1",
        payload={},
        reply_context={"req_id": "r1"},
    )
    db_session.add(ev)
    await db_session.commit()
    # 下面那次 rollback 会过期所有实例（与 expire_on_commit 无关），事后再取 .id 就成了同步懒加载。
    bot_id, ev_id = bot.id, ev.id
    db_session.add(
        InboundEvent(
            bot_id=bot.id,
            platform="wecom",
            platform_msg_id="m1",
            kind="message",
            chat_type="single",
            chat_id="u1",
            payload={},
            reply_context={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    task = Task(bot_id=bot_id, kind="chat", session_key="u1", payload={}, inbound_event_id=ev_id)
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskStream(
            task_id=task.id,
            bot_id=bot_id,
            platform="wecom",
            stream_id="s1",
            reply_context={"req_id": "r1"},
            lease_generation=1,
            running_since=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await db_session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
    await db_session.commit()
    assert (await db_session.execute(select(TaskStream))).scalar_one_or_none() is None


async def test_lease_outbox_session_reached_and_log(db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    inst = ProcessInstance(
        id="gateway-wecom-a:h:1:1",
        service="gateway-wecom",
        version="dev",
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
    )
    db_session.add(inst)
    # 总线表之间不挂 relationship，UOW 就不会按外键排插入顺序：租约先于实例会撞外键。
    await db_session.flush()
    db_session.add(BotLease(bot_id=bot.id, platform="wecom", holder_instance=inst.id))
    db_session.add(
        OutboxItem(
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key="1:send:0",
            target={"chat_id": "u1"},
            payload={"markdown": "hi"},
        )
    )
    sid = uuid.uuid4()
    db_session.add(
        ChatSession(bot_id=bot.id, session_key="u1", relay_session_id=sid, backend="claude")
    )
    db_session.add(UserReached(bot_id=bot.id, user_id=bot.created_by, platform_chat_id="u1"))
    db_session.add(
        ChatLog(
            bot_id=bot.id,
            bot_key=bot.bot_key,
            platform="wecom",
            chat_type="single",
            message_type="text",
            status="success",
            request_at=datetime.now(UTC),
            message_content="hi",
            response_content="ok",
        )
    )
    await db_session.commit()
    lease = (await db_session.execute(select(BotLease))).scalar_one()
    assert lease.generation == 0 and lease.connection_state == "disconnected"
    ob = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert ob.status == "pending" and ob.attempts == 0
    await db_session.execute(text("DELETE FROM process_instances WHERE id = :id"), {"id": inst.id})
    await db_session.commit()
    await db_session.refresh(lease)
    assert lease.holder_instance is None
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.tools_used == [] and log.input_tokens is None
