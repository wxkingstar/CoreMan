"""目标员工那边的一轮：以所有者的身份、用不落库的替身、不往飞书发任何东西，记录标为私有。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.kinds import BOT_HELP_KIND, JOB_RESULT_KIND
from coreman.core.bus import streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import ChatSession, Task
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler import reaper
from coreman.runtime.worker.assistant_bot_help import AssistantBotHelpHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.integration.assistant_helpers import raw
from tests.integration.dispatch_helpers import (
    QUESTION,
    asked,
    help_task,
    rows_in,
    run_help,
    target_bot,
)
from tests.integration.job_helpers import reloaded, take, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant, streams_of
from tests.integration.worker_helpers import user_input

pytestmark = pytest.mark.usefixtures("assistant_master")
# 这一轮不该留下新行的表：出站、入站事件、待答状态、私聊可达、公共任务、凭证表单、已发消息存档。
QUIET = (
    "outbox",
    "inbound_events",
    "interaction_states",
    "user_reached",
    "work_tasks",
    "credential_requests",
    "feishu_sent_messages",
)
LOGGED = (
    "SELECT platform || '|' || chat_type || '|' || message_type || '|' || private::text"
    " || '|' || user_id::text || '|' || session_key FROM chat_logs WHERE task_id = :id"
)


async def _counts(session: AsyncSession) -> dict[str, int]:
    return {table: await rows_in(session, table) for table in QUIET}


async def test_the_round_runs_as_the_owner_and_says_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    before = await _counts(db_session)
    done, relay = await run_help(db_engine, db_session, task)
    assert done.status == "succeeded" and done.bot_id == target.id
    (request,) = relay.requests
    text = user_input(request)
    assert QUESTION in text and "个人助理" in text
    # 流只留在库里：回复上下文带 `_silent`，会话坐标是合成的，不是所有者的任何一个会话。
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["_silent"] is True
    assert stream.reply_context["chat_id"] == row.session_key and stream.is_complete
    assert await _counts(db_session) == before
    # 对话记录记在目标员工名下、归所有者、标为私有；类型就是任务类型。
    assert await raw(db_session, LOGGED, id=task.id) == (
        f"feishu|single|{BOT_HELP_KIND}|true|{owner.id}|{row.session_key}"
    )
    # 独立会话：目标员工名下只多了这一个会话键。
    chat = await db_session.get(ChatSession, (target.id, row.session_key))
    assert chat is not None and chat.last_speaker_user_id == owner.id
    assert (await reloaded(db_session, row)).started_at is not None


async def test_a_long_round_sends_no_completion_notice(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    task = await help_task(db_session, await asked(db_session, bot, target))
    handler = AssistantBotHelpHandler()
    # 把「长任务」的门槛降到零：普通轮次这时会往会话里发一条完成提醒。
    handler.LONG_TASK_SECONDS = 0
    done, _ = await run_help(db_engine, db_session, task, handler=handler)
    assert done.status == "succeeded" and await rows_in(db_session, "outbox") == 0


async def test_the_handler_never_offers_cards_or_prose_formatting() -> None:
    handler = AssistantBotHelpHandler()
    turn = SimpleNamespace(task=SimpleNamespace(payload={}))
    assert handler._structured_reply(turn) is True  # type: ignore[arg-type]
    assert handler._needs_content() is False
    verdict = object()
    # 触限时不发切换卡、不写待答状态：原样返回。
    assert await handler._postprocess_rate_limit(None, None, verdict) == (verdict, None)  # type: ignore[arg-type]
    plain = ChatTaskHandler()
    assert plain._structured_reply(turn) is False  # type: ignore[arg-type]
    helper = SimpleNamespace(task=SimpleNamespace(payload={"collaboration_phase": "helper"}))
    assert plain._structured_reply(helper) is True  # type: ignore[arg-type]


async def test_access_lost_after_dispatch_is_refused_and_reported(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    target.accept_bot_help = False
    await db_session.commit()
    task = await help_task(db_session, row)
    done, relay = await run_help(db_engine, db_session, task)
    assert (done.status, done.error_code) == ("cancelled", "bot_help_not_allowed")
    assert relay.requests == [] and await streams_of(db_session, task) == []
    failed = await reloaded(db_session, row)
    assert (failed.status, failed.error_code) == ("failed", "bot_help_not_allowed")
    # 立刻投回助理主会话，不等对账。
    (result,) = await tasks_of(db_session, bot, JOB_RESULT_KIND)
    assert result.session_key == "oc_private" and failed.result_task_id == result.id
    assert await rows_in(db_session, "outbox") == 0


async def test_a_task_without_its_job_is_cancelled(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    other = await target_bot(db_session, key="demo-bot-b", name="另一位")
    row = await asked(db_session, bot, target)
    forged = [
        # 编号不存在。
        NewTask(
            bot_id=target.id,
            kind=BOT_HELP_KIND,
            user_id=owner.id,
            session_key=f"assistant-help:{uuid.uuid4()}",
            payload={"job_id": str(uuid.uuid4()), "bot_key": "demo-bot", "platform_user_id": "x"},
        ),
        # 编号是真的，但这一行问的是另一位员工、在途的也不是这个任务。
        NewTask(
            bot_id=other.id,
            kind=BOT_HELP_KIND,
            user_id=owner.id,
            session_key=row.session_key,
            payload={"job_id": str(row.id), "bot_key": "demo-bot-b", "platform_user_id": "x"},
        ),
    ]
    for new in forged:
        queued = await tasks.enqueue(db_session, new)
        await db_session.commit()
        assert queued is not None
        done, relay = await run_help(db_engine, db_session, await take(db_session, queued.id))
        assert (done.status, done.error_code) == ("cancelled", "bot_help_inactive")
        assert relay.requests == []
    assert (await reloaded(db_session, row)).status == "queued"


async def test_the_reaper_stays_quiet_and_keeps_the_record_private(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    task = await help_task(db_session, await asked(db_session, bot, target))
    reached = await rows_in(db_session, "user_reached")
    now = datetime.now(UTC)
    lost = await db_session.get(Task, task.id, populate_existing=True)
    assert lost is not None
    lost.heartbeat_at, lost.claimed_at = None, now - timedelta(seconds=61)
    await db_session.commit()
    assert (
        await reaper.reap_lost_tasks(
            db_session, now, chat_logs_factory=make_session_factory(db_engine)
        )
        == 1
    )
    await db_session.commit()
    # 没开流就失联的一轮：普通私聊这时会往会话里补发「进程没了」，并把这个人记成私聊过它。
    assert await rows_in(db_session, "outbox") == 0
    assert await rows_in(db_session, "user_reached") == reached
    assert await raw(db_session, LOGGED, id=task.id) == (
        f"feishu|single|{BOT_HELP_KIND}|true|{owner.id}|assistant-help:{task.payload['job_id']}"
    )


async def test_a_quota_notice_fails_the_round_without_a_switch_card(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    before = await _counts(db_session)
    # 普通私聊这时会接上额度表、发「切过去吗」的卡片并写一条待答状态。
    done, _ = await run_help(db_engine, db_session, task, "rate_limit")
    assert (done.status, done.error_code) == ("failed", "rate_limited")
    assert await _counts(db_session) == before
    failed = await reloaded(db_session, row)
    assert (failed.status, failed.error_code, failed.result_enc) == ("failed", "rate_limited", None)
    assert len(await tasks_of(db_session, bot, JOB_RESULT_KIND)) == 1


async def test_a_round_lost_after_opening_is_closed_quietly(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """开了流、写了半截之后 worker 失联：流补上终态，但不往任何会话里补发，记录仍是私有的。"""
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    row = await asked(db_session, bot, target)
    task = await help_task(db_session, row)
    await tasks.start(db_session, task.id)
    for mode in ("stream", "proactive"):
        if mode == "proactive":
            # 第二种：流已不在推（主动模式），普通轮次这时改走出站箱。
            other = await asked(db_session, bot, target, question="再查一件")
            task = await help_task(db_session, other)
            await tasks.start(db_session, task.id)
        await streams.create(
            db_session,
            task_id=task.id,
            bot_id=target.id,
            platform="feishu",
            stream_id=f"s-help-{mode}",
            reply_context={"chat_id": task.session_key, "chat_type": "single", "_silent": True},
            lease_generation=1,
            running_since=datetime.now(UTC),
            delivery_mode=mode,
        )
        await streams.update(db_session, task.id, pending_text="查到一半", bot_id=target.id)
        lost = await db_session.get(Task, task.id, populate_existing=True)
        assert lost is not None
        lost.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        await db_session.commit()
        reached = await rows_in(db_session, "user_reached")
        now = datetime.now(UTC)
        assert (
            await reaper.reap_lost_tasks(
                db_session, now, chat_logs_factory=make_session_factory(db_engine)
            )
            == 1
        )
        await db_session.commit()
        (stream,) = await streams_of(db_session, task)
        assert stream.is_complete
        assert await rows_in(db_session, "outbox") == 0
        assert await rows_in(db_session, "user_reached") == reached
        assert await raw(db_session, LOGGED, id=task.id) == (
            f"feishu|single|{BOT_HELP_KIND}|true|{owner.id}|{task.session_key}"
        )
    # 流恢复也不补发：任务早已结束、流没收尾的那种。
    await db_session.execute(
        text("UPDATE task_streams SET is_complete = false, finish_pushed_at = now()")
    )
    await db_session.execute(text("UPDATE tasks SET finished_at = now() - interval '5 minutes'"))
    await db_session.commit()
    assert await reaper.recover_terminal_streams(db_session, datetime.now(UTC)) == 2
    await db_session.commit()
    assert await rows_in(db_session, "outbox") == 0
