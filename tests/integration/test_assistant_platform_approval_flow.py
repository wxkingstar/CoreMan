"""平台来源的轮次里提交的写操作，从提交到结果回合开轮的整条路；以及伪造的结果任务到不了模型。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND, HANDOVER_KIND
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Task
from coreman.core.feishu_personal import policy
from tests.integration.approval_helpers import decided, install, pending, queued
from tests.integration.test_assistant_approval_result import run_turn
from tests.integration.test_assistant_platform_turn_handler import assistant as platform_assistant
from tests.integration.test_assistant_platform_turn_handler import queued as claimed_turn
from tests.integration.test_assistant_platform_turn_handler import streams_of
from tests.integration.worker_helpers import user_input

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_a_write_submitted_in_a_platform_turn_comes_back_to_the_private_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整条路：平台来源的一轮里提交写操作 → 单上没有入站事件 → 决定送达后经平台来源入队 →
    结果回合在所有者的私聊里开轮。"""
    install(monkeypatch)
    bot, owner_user = await platform_assistant(db_session)
    turn = await claimed_turn(db_session, bot, HANDOVER_KIND, {"note": "李四问报价"})
    scope = await policy.task_scope(db_session, turn.id, str(owner_user.id))
    assert scope.platform and scope.event is None
    row = await pending(db_session, scope)
    assert row.origin_event_id is None and row.session_key == "oc_private"
    await tasks.finish(db_session, turn.id, status="succeeded")
    await db_session.commit()
    found = await decided(db_session, row, "rejected", "先别发")
    assert found.known and found.queued
    (task,) = await queued(db_session, APPROVAL_RESULT_KIND)
    assert task.inbound_event_id is None and task.session_key == "oc_private"
    assert task.user_id == owner_user.id and task.payload["serialize_session"] is True
    done, relay = await run_turn(db_engine, db_session)
    assert done.status == "succeeded" and done.id == task.id
    text = user_input(relay.requests[0])
    assert f"审批单 {row.id}" in text and "被拒绝，没有执行" in text
    # 结果回合同样是平台来源的一轮：拿得到所有者的个人工具，回复发到私聊。
    assert "COREMAN_FEISHU_PERSONAL_TOKEN" in relay.requests[0]["env_vars"]
    (stream,) = await streams_of(db_session, task)
    assert stream.reply_context["chat_id"] == "oc_private"


async def test_a_forged_result_without_an_event_never_reaches_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """助理上凭空多出一行没有入站事件的结果任务（总线不收，这里当成库里已经有了）：指不出
    一张结局相符的本助理审批单，开轮前就取消，不调模型、不发能力令牌。"""
    bot, owner_user = await platform_assistant(db_session)
    with pytest.raises(RuntimeError):
        await tasks.enqueue(
            db_session,
            NewTask(
                bot_id=bot.id,
                kind=APPROVAL_RESULT_KIND,
                user_id=owner_user.id,
                session_key="oc_private",
                payload={"approval_id": str(uuid.uuid4()), "status": "approved"},
            ),
        )
    forged = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            user_id=owner_user.id,
            session_key="oc_private",
            payload={"approval_id": str(uuid.uuid4()), "status": "approved"},
        ),
    )
    assert forged is not None
    await db_session.execute(
        update(Task).where(Task.id == forged.id).values(kind=APPROVAL_RESULT_KIND)
    )
    await db_session.commit()
    done, relay = await run_turn(db_engine, db_session)
    assert done.id == forged.id
    assert done.status == "cancelled" and done.error_code == "approval_result_inactive"
    assert relay.requests == [] and await streams_of(db_session, done) == []
