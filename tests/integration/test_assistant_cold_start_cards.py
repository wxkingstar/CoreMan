"""冷启动卡片的回调、整个跳过，以及 2F 上线前开通的助理在下次私聊时开始冷启动。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import cold_start, notes, settings
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    OutboxItem,
    Task,
    User,
)
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.integration.assistant_helpers import SECRET, identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import last_sent, say
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import MASTER, build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
CIPHER = Cipher(MASTER)
MESSAGE = "om_cold_start_card"


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def asked(
    session: AsyncSession, step: str = "people"
) -> tuple[Bot, AssistantSettings, OutboxItem]:
    """冷启动走到 `step`，这一题的卡片已经发到私聊。"""
    bot, _ = await assistant(session)
    now = utcnow()
    assert await cold_start.begin(session, bot.id, now)
    row = await settings.get(session, bot.id)
    row.cold_start_step = step
    await cold_start.send(session, CIPHER, bot, row, step, now, groups=[("oc_team", "报价群")])
    await session.commit()
    sent = await last_sent(session, bot)
    assert sent is not None
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": MESSAGE}
    await session.commit()
    return bot, row, sent


def button(sent: OutboxItem, verb: str) -> dict[str, Any]:
    elements = sent.payload["card"]["body"]["elements"]
    found = elements[1]["elements"][-1] if verb == "answer" else elements[2]
    value: dict[str, Any] = found["behaviors"][0]["value"]
    return value


async def click(
    session: AsyncSession,
    bot: Bot,
    sent: OutboxItem,
    verb: str,
    *,
    who: str = "owner_pid",
    selected: dict[str, list[str]] | None = None,
    **edits: Any,
) -> Task:
    """点了卡片上的按钮：回调按网关的样子落库并排成任务。"""
    action = {**button(sent, verb), "card_type": "form", "selected": selected or {}, **edits}
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=f"action:{uuid.uuid4().hex}",
        kind="card_action",
        chat_type="single",
        chat_id="oc_private",
        sender_platform_user_id=who,
        sender_open_id=f"ou_{who}",
        reply_context={"chat_id": "oc_private", "message_id": MESSAGE},
        payload={},
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": who},
            session_key="oc_private",
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await session.commit()
    return task


async def result_of(engine: AsyncEngine, session: AsyncSession, task: Task) -> Any:
    await CardActionHandler().run(build_ctx(engine, task))
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.status == "succeeded"
    return (done.result or {}).get("card")


async def reloaded(session: AsyncSession, bot: Bot) -> AssistantSettings:
    row = await session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    return row


async def test_the_owner_answers_a_question(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, sent = await asked(db_session)
    task = await click(
        db_session, bot, sent, "answer", selected={"answer": [f"上级是李四 {SECRET}"]}
    )
    assert await result_of(db_engine, db_session, task) == "cold_start_answered"
    page = await notes.read(db_session, bot.id, "profile/people.md")
    assert page is not None and page.pinned and SECRET in page.body
    row = await reloaded(db_session, bot)
    assert row.cold_start_step == "duties" and row.cold_start_next_at is not None
    update = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "card_update")
    )
    assert update is not None and update.target["message_id"] == MESSAGE
    # 换上去的卡片没有按钮，也不回显答案。
    assert "button" not in str(update.payload) and SECRET not in str(update.payload)
    assert "profile/people.md" in str(update.payload)


async def test_clicking_the_same_card_again_changes_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, sent = await asked(db_session)
    first = await click(db_session, bot, sent, "answer", selected={"answer": ["上级是李四"]})
    assert await result_of(db_engine, db_session, first) == "cold_start_answered"
    again = await click(db_session, bot, sent, "answer", selected={"answer": ["上级是王五"]})
    assert await result_of(db_engine, db_session, again) == "cold_start_stale"
    skipped = await click(db_session, bot, sent, "skip")
    assert await result_of(db_engine, db_session, skipped) == "cold_start_stale"
    page = await notes.read(db_session, bot.id, "profile/people.md")
    assert page is not None and page.body == "上级是李四" and page.version == 1
    assert (await reloaded(db_session, bot)).cold_start_step == "duties"


async def test_skipping_one_question(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _, sent = await asked(db_session, "focus")
    task = await click(db_session, bot, sent, "skip")
    assert await result_of(db_engine, db_session, task) == "cold_start_skipped"
    row = await reloaded(db_session, bot)
    assert row.cold_start_step == "hours" and row.focus_chats_enc is None


async def test_the_last_answer_finishes_the_cold_start(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, sent = await asked(db_session, "hours")
    task = await click(
        db_session,
        bot,
        sent,
        "answer",
        selected={"hours": ["09:00-18:00"], "timezone": ["Asia/Shanghai"]},
    )
    assert await result_of(db_engine, db_session, task) == "cold_start_answered"
    row = await reloaded(db_session, bot)
    assert row.cold_start_done_at is not None and row.cold_start_outcome == "completed"
    assert (row.work_start, row.work_end) == ("09:00", "18:00")


async def test_only_the_owner_with_a_platform_signed_token_counts(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, sent = await asked(db_session)
    db_session.add(User(login_name="lisi", display_name="李四"))
    await db_session.commit()
    step, verb, mac = button(sent, "answer")["level"].split(".")
    for who, edits in (
        ("lisi", {}),  # 不是所有者
        ("owner_pid", {"level": f"{step}.{verb}.{'0' * 64}"}),  # 令牌不是平台签的
        ("owner_pid", {"level": f"duties.{verb}.{mac}"}),  # 拿这一题的令牌答另一题
        ("owner_pid", {"level": f"{step}.{verb}.停用"}),  # 非 ASCII
    ):
        task = await click(
            db_session, bot, sent, "answer", who=who, selected={"answer": ["上级是王五"]}, **edits
        )
        assert await result_of(db_engine, db_session, task) == "ignored", (who, edits)
    assert await notes.read(db_session, bot.id, "profile/people.md") is None
    assert (await reloaded(db_session, bot)).cold_start_step == "people"


async def test_a_card_does_nothing_once_the_assistant_is_being_stopped(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, sent = await asked(db_session)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    task = await click(db_session, bot, sent, "answer", selected={"answer": ["上级是李四"]})
    assert await result_of(db_engine, db_session, task) == "ignored"
    assert await notes.read(db_session, bot.id, "profile/people.md") is None


async def test_the_owner_skips_the_whole_cold_start(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await asked(db_session, "duties")
    done, relay = await say(db_engine, db_session, bot, "跳过")
    assert done.status == "succeeded" and done.result == {"assistant": "cold_start_skipped"}
    assert relay.requests == []
    row = await reloaded(db_session, bot)
    assert row.cold_start_done_at is not None and row.cold_start_outcome == "skipped"
    assert row.cold_start_step is None
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.payload["markdown"].startswith("好的，先跳过。")
    # 冷启动已经结束：「跳过」是一句普通的话。
    done, relay = await say(db_engine, db_session, bot, "/跳过")
    assert (done.result or {}).get("assistant") is None and len(relay.requests) == 1


async def test_an_assistant_set_up_earlier_starts_on_the_next_private_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """迁移不回填：所有者下一次来私聊时才开始，那条消息照常交给模型。"""
    bot, _ = await assistant(db_session)
    row = await settings.get(db_session, bot.id)
    assert settings.cold_start_state(row) == "pending"
    done, relay = await say(db_engine, db_session, bot, "帮我看看今天的日程")
    assert done.status == "succeeded" and len(relay.requests) == 1
    row = await reloaded(db_session, bot)
    assert row.cold_start_started_at is not None and row.cold_start_step == "voice"
    assert row.cold_start_next_at is not None and row.cold_start_done_at is None
    started = row.cold_start_started_at
    # 之后的消息不重新开始。
    await say(db_engine, db_session, bot, "再看看明天的")
    assert (await reloaded(db_session, bot)).cold_start_started_at == started


async def test_no_start_without_a_recorded_identity_or_from_someone_else(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    await say(db_engine, db_session, bot, "你好")
    # 没记下身份：冷启动不开始（开轮时按默认值建出的设置行上没有身份）。
    bare = await db_session.get(AssistantSettings, bot.id, populate_existing=True)
    assert bare is None or (bare.cold_start_started_at is None and not settings.identified(bare))
    await identified(db_session, bot)
    await say(db_engine, db_session, bot, "你好", sender="lisi", chat_id="oc_lisi")
    assert (await reloaded(db_session, bot)).cold_start_started_at is None
