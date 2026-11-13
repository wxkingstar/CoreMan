"""平台来源的一轮在 worker 里：发言者是所有者，文本由平台给，回复发到所有者的私聊，没有入站事件。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, HANDOVER_KIND
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    InboundEvent,
    RelayServer,
    RuntimeNode,
    Task,
    TaskStream,
    User,
)
from coreman.core.feishu_personal import policy
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.platform_turn import PlatformTurnHandler, stand_in
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import identified, raw
from tests.integration.lifecycle_helpers import active_assistant
from tests.integration.test_assistant_approval_result import _row
from tests.integration.test_chat_handler import chat_task
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


class Handover(PlatformTurnHandler):
    """用例里的最小子类：文本取自载荷，没有内容就没有这一轮。"""

    kind = HANDOVER_KIND

    async def _text(self, session: AsyncSession, ctx: TaskContext, bot: Bot) -> str | None:
        note = ctx.task.payload.get("note")
        return f"交回事项：{note}" if note else None


async def assistant(session: AsyncSession) -> tuple[Bot, User]:
    """开通好、记下了身份的助理；所在节点支持个人工具。"""
    bot, owner, _ = await active_assistant(session)
    await identified(session, bot)
    relay = await session.get(RelayServer, bot.relay_server_id)
    assert relay is not None
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        policy.RUNTIME_CAPABILITY: True,
    }
    node.capabilities = capabilities
    await session.commit()
    return bot, owner


async def queued(session: AsyncSession, bot: Bot, kind: str, payload: dict[str, Any]) -> Task:
    task = await platform_turns.enqueue(session, bot, kind, payload, dedupe_key=f"{kind}:1")
    await session.commit()
    assert task is not None
    claimed = await claim(session)
    assert claimed.id == task.id
    return claimed


async def run(
    engine: AsyncEngine, session: AsyncSession, task: Task, handler: PlatformTurnHandler
) -> tuple[Task, FakeRelay]:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done, relay


async def streams_of(session: AsyncSession, task: Task) -> list[TaskStream]:
    return list(await session.scalars(select(TaskStream).where(TaskStream.task_id == task.id)))


async def test_the_turn_runs_as_the_owner_in_the_private_chat(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    events = await db_session.scalar(select(func.count()).select_from(InboundEvent))
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "succeeded"
    assert "交回事项：李四问报价" in user_input(relay.requests[0])
    # 和所有者的私聊一轮一样拿到个人工具的能力令牌。
    assert "COREMAN_FEISHU_PERSONAL_TOKEN" in relay.requests[0]["env_vars"]
    # 回复作为一条新消息发到所有者的私聊：有会话坐标，没有被回复的消息。
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private"
    assert stream.reply_context["chat_type"] == "single"
    assert "message_id" not in stream.reply_context
    logged = await raw(
        db_session,
        "SELECT session_key || '|' || message_type || '|' || user_id::text"
        " FROM chat_logs WHERE task_id = :id",
        id=task.id,
    )
    assert logged == f"oc_private|{HANDOVER_KIND}|{owner.id}"
    # 替身不落库。
    assert await db_session.scalar(select(func.count()).select_from(InboundEvent)) == events


async def test_nothing_to_say_cancels_without_calling_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {})
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "cancelled" and done.error_code == "platform_turn_inactive"
    assert relay.requests == [] and await streams_of(db_session, task) == []


async def test_a_paused_assistant_fails_the_turn_without_a_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """所有者没有刚发来什么：暂停时不为平台自己发起的一轮私信，只结掉任务。"""
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    relay_row = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay_row is not None
    relay_row.assistant_pool = False
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status == "failed" and done.error_code == "assistant_paused"
    assert relay.requests == [] and await streams_of(db_session, task) == []


@pytest.mark.parametrize("change", ["foreign_session", "identity_gone", "deprovisioning"])
async def test_a_turn_that_no_longer_belongs_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession, change: str
) -> None:
    bot, _ = await assistant(db_session)
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "x"})
    if change == "foreign_session":
        task.session_key = "oc_other"
    elif change == "identity_gone":
        await db_session.execute(update(AssistantSettings).values(chat_id=""))
    else:
        bot.assistant_state = "deprovisioning"
    await db_session.commit()
    done, relay = await run(db_engine, db_session, task, Handover())
    assert done.status in ("cancelled", "failed") and relay.requests == []
    assert await streams_of(db_session, task) == []


async def test_an_approval_result_without_an_inbound_event_runs_on_the_stand_in(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """平台来源的轮次里提交的审批单没有入站事件：结果回合照样跑，文本照旧由平台写。"""
    bot, _ = await assistant(db_session)
    row = _row(bot.id, "rejected", reason_enc="先别发", session_key="oc_private")
    db_session.add(row)
    await db_session.flush()
    task = await queued(
        db_session, bot, APPROVAL_RESULT_KIND, {"approval_id": str(row.id), "status": "rejected"}
    )
    assert task.inbound_event_id is None
    done, relay = await run(db_engine, db_session, task, AssistantApprovalResultHandler())
    assert done.status == "succeeded"
    text = user_input(relay.requests[0])
    assert f"审批单 {row.id}" in text and "被拒绝，没有执行" in text
    assert "<approver_reason>\n先别发\n</approver_reason>" in text
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private"


def test_the_stand_in_carries_only_coordinates() -> None:
    bot = Bot(id=uuid.uuid4())
    row = AssistantSettings(
        bot_id=bot.id, chat_id="oc_private", platform_user_id="owner_pid", open_id="ou_owner"
    )
    event = stand_in(bot, row, Task(id=7))
    assert event.id is None and event.bot_id == bot.id and event.payload == {}
    assert (event.platform, event.kind, event.chat_type) == ("feishu", "message", "single")
    assert (event.chat_id, event.sender_platform_user_id, event.sender_open_id) == (
        "oc_private",
        "owner_pid",
        "ou_owner",
    )
    assert event.platform_msg_id == "platform-turn:7"
    assert event.reply_context == {"chat_id": "oc_private", "chat_type": "single"}


async def test_a_stand_in_turn_is_not_the_owner_reaching_out(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """替身不是所有者发来的私聊消息：不写 user_reached，也不去处理「被点过的回复按钮」。"""
    from coreman.core.chat import card_replies

    used: list[Any] = []

    async def record(*args: Any) -> None:
        used.append(args)

    monkeypatch.setattr(card_replies, "enqueue_used", record)
    bot, _ = await assistant(db_session)
    await db_session.execute(text("DELETE FROM user_reached"))
    task = await queued(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    done, _ = await run(db_engine, db_session, task, Handover())
    assert done.status == "succeeded"
    assert await raw(db_session, "SELECT count(*) FROM user_reached") == 0
    assert used == []
    # 所有者真的发来一条时照旧记下。
    message = await chat_task(
        db_session, bot, "在吗", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    relay = FakeRelay("normal")
    ctx = build_ctx(db_engine, message, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert await raw(db_session, "SELECT count(*) FROM user_reached") == 1
    assert len(used) == 1
