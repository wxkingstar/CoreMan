"""平台来源的轮次入队：只给开通好、记下了身份的助理，任务不带入站事件、归所有者、排在会话里。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import (
    APPROVAL_EXECUTE_KIND,
    APPROVAL_RESULT_KIND,
    BOT_HELP_KIND,
    HANDOVER_KIND,
    PLATFORM_TURN_KINDS,
    SUBTASK_KIND,
)
from coreman.core.db.models import TASK_KINDS, AssistantSettings, Task
from tests.integration.assistant_helpers import (
    SECRET,
    identified,
    raw,
    seed_assistant,
    seed_standard,
)

pytestmark = pytest.mark.usefixtures("assistant_master")


def test_kinds_are_registered() -> None:
    assert set(PLATFORM_TURN_KINDS) | {BOT_HELP_KIND} <= set(TASK_KINDS)
    assert platform_turns.ORIGIN_KINDS == (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)


def test_session_ownership() -> None:
    row = AssistantSettings(chat_id="oc_private")
    assert platform_turns.owns_session(row, "oc_private")
    assert platform_turns.owns_session(row, "oc_private#job:7")
    for foreign in (None, "", "oc_other", "oc_private_2", "oc_private#topic:1"):
        assert not platform_turns.owns_session(row, foreign), foreign
    assert not platform_turns.owns_session(AssistantSettings(chat_id=""), "")


def test_platform_origin_needs_a_listed_kind_and_no_inbound_event() -> None:
    assert platform_turns.is_platform(Task(kind=HANDOVER_KIND, inbound_event_id=None))
    assert platform_turns.is_platform(Task(kind=APPROVAL_RESULT_KIND, inbound_event_id=None))
    assert not platform_turns.is_platform(Task(kind=APPROVAL_RESULT_KIND, inbound_event_id=3))
    assert not platform_turns.is_platform(Task(kind="chat", inbound_event_id=None))
    assert not platform_turns.is_platform(Task(kind=BOT_HELP_KIND, inbound_event_id=None))


async def test_enqueue_builds_an_owner_task_in_the_main_session(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await identified(db_session, bot)
    task = await platform_turns.enqueue(
        db_session,
        bot,
        HANDOVER_KIND,
        # 调用方给的同名键不算数。
        {"note": SECRET, "serialize_session": False, "platform_user_id": "someone_else"},
        dedupe_key="handover:1",
    )
    await db_session.commit()
    assert task is not None and task.kind == HANDOVER_KIND and task.status == "queued"
    assert task.user_id == owner.id and task.inbound_event_id is None
    assert task.session_key == "oc_private"
    assert task.payload == {
        "note": SECRET,
        "serialize_session": True,
        "bot_key": bot.bot_key,
        "platform_user_id": "owner_pid",
    }
    # 载荷按所有者的数据密钥封存，排队用的键留明文。
    stored = str(
        await raw(db_session, "SELECT payload::text FROM tasks WHERE id = :id", id=task.id)
    )
    assert SECRET not in stored and '"serialize_session": true' in stored
    # 同一个去重键只入队一次。
    again = await platform_turns.enqueue(
        db_session, bot, HANDOVER_KIND, {"note": "x"}, dedupe_key="handover:1"
    )
    assert again is None


async def test_enqueue_accepts_a_job_session_and_refuses_a_foreign_one(
    db_session: AsyncSession,
) -> None:
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    job = await platform_turns.enqueue(
        db_session, bot, SUBTASK_KIND, {}, session_key="oc_private#job:3", dedupe_key="j3"
    )
    assert job is not None and job.session_key == "oc_private#job:3"
    foreign = await platform_turns.enqueue(
        db_session, bot, HANDOVER_KIND, {}, session_key="oc_someone_else", dedupe_key="f1"
    )
    assert foreign is None


@pytest.mark.parametrize("state", ["provisioning", "deprovisioning", "deprovisioned", "disabled"])
async def test_enqueue_refuses_an_assistant_that_is_not_active(
    db_session: AsyncSession, state: str
) -> None:
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    if state == "disabled":
        bot.enabled = False
    else:
        bot.assistant_state = state
    await db_session.commit()
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="k") is None


async def test_enqueue_refuses_without_a_recorded_identity(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="a") is None
    await identified(db_session, bot, open_id="")
    assert await platform_turns.enqueue(db_session, bot, HANDOVER_KIND, {}, dedupe_key="b") is None


async def test_enqueue_refuses_standard_bots_and_other_kinds(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    assert (
        await platform_turns.enqueue(db_session, standard, HANDOVER_KIND, {}, dedupe_key="s")
        is None
    )
    bot, _ = await seed_assistant(db_session)
    await identified(db_session, bot)
    for kind in ("chat", "cron_run", APPROVAL_EXECUTE_KIND, BOT_HELP_KIND):
        with pytest.raises(ValueError, match="platform turn kind"):
            await platform_turns.enqueue(db_session, bot, kind, {}, dedupe_key=f"x:{kind}")
