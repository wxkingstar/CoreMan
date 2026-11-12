"""端到端：开通 → 授权 → 私聊里一轮调用写工具 → 审批单 → 批准 → 执行 → 结果回会话 → 停用 → 库里的
内容不可再解密。飞书、审批系统、节点与模型都用替身；网关入站、看护进程、worker 走真实代码。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import approvals, keys, sealed
from coreman.core.assistants.approvals import execute
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import instances, tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    FeishuPersonalGrant,
    InboundEvent,
    OutboxItem,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration import approval_helpers
from tests.integration.approval_helpers import CFG, TEXT, decided, queued, reply
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.lifecycle_helpers import (
    APP_ID,
    CHAT,
    OWNER_OPEN_ID,
    OWNER_PID,
    TENANT,
    FakeMessenger,
    assistant_app,
    click_event,
    flow_of,
    member,
    message_event,
    pool_relay,
    tick,
)
from tests.integration.test_assistant_gateway_provisioning import gateway
from tests.integration.test_assistant_provisioning import install, poll_now
from tests.integration.test_assistant_stop_command import click as stop_click
from tests.integration.worker_helpers import MASTER, build_ctx, user_input

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def claimed(session: AsyncSession, kind: str) -> Task:
    """worker 认领下一条任务（私聊消息有几秒的合并窗口，这里不等）。"""
    await session.execute(update(Task).where(Task.status == "queued").values(run_after=func.now()))
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    lane = "fast" if kind == "card_action" else "normal"
    task = await tasks.claim(session, lane=lane, instance_id="worker-test")
    await session.commit()
    assert task is not None and task.kind == kind, task
    return task


async def turn(engine: AsyncEngine, task: Task, handler=None) -> FakeRelay:  # type: ignore[no-untyped-def]
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await (handler or ChatTaskHandler()).run(ctx)
    await ctx.chat_logs.drain(5)
    return relay


async def test_provision_use_approve_execute_and_deprovision(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    feishu, tower = approval_helpers.install(monkeypatch)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    app = await assistant_app(db_session)
    owner = await member(db_session, open_id=OWNER_OPEN_ID)
    await pool_relay(db_session)
    await db_session.commit()
    inbound = gateway(db_engine, app.id)

    # ---- 开通 --------------------------------------------------------------------------------
    await inbound.accept(message_event())
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.offer_message_id is not None
    assert await inbound.accept(click_event(messenger.token(), flow.offer_message_id)) == (
        "provision_started"
    )
    for _ in range(4):
        await tick(factory, messenger, later=6)
    auth.approve()
    await poll_now(db_session)
    await tick(factory, messenger, later=12)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.bot_id is not None
    bot = await db_session.get(Bot, flow.bot_id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"

    # ---- 私聊里的一轮：派到池节点的沙箱里 -------------------------------------------------------
    await inbound.accept(message_event())
    task = await claimed(db_session, "chat")
    relay = await turn(db_engine, task)
    body = relay.requests[0]
    assert body["sandbox"] is True and body["transcript"]["restore"] is False
    # 这一轮里助理调用写工具：不执行，提交审批。
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    scope = Scope(task, bot, owner.id, TENANT, APP_ID, OWNER_OPEN_ID, OWNER_PID, CHAT, event)
    submitted = await approvals.submit(
        db_session,
        None,
        scope,
        "feishu_reply_message",
        reply(),
        cfg=CFG,  # type: ignore[arg-type]
    )
    await db_session.commit()
    assert submitted["status"] == "pending_approval" and feishu.writes == []
    row = await db_session.get(AssistantApproval, submitted["approval_id"])
    assert row is not None and row.status == "pending" and tower.submitted

    # ---- 批准 → 平台原样执行 → 结果作为一轮回到助理会话 --------------------------------------
    await decided(db_session, row)
    (execute_task,) = await queued(db_session, APPROVAL_EXECUTE_KIND)
    assert await execute.run(factory, Cipher(MASTER), row.id, execute_task, CFG) == "executed"
    assert [path for _, path, _ in feishu.writes] == ["/im/v1/messages/om_2/reply"]
    assert TEXT in feishu.writes[0][2]["content"]
    await db_session.execute(
        update(Task).where(Task.kind == APPROVAL_EXECUTE_KIND).values(status="succeeded")
    )
    await db_session.commit()
    result_task = await claimed(db_session, APPROVAL_RESULT_KIND)
    result_relay = await turn(db_engine, result_task, AssistantApprovalResultHandler())
    assert "已批准，平台已按提交时的参数原样执行" in user_input(result_relay.requests[0])
    assert result_relay.requests[0]["sandbox"] is True

    # ---- 停用 ----------------------------------------------------------------------------------
    stop = message_event()
    stop["event"]["message"]["content"] = '{"text": "停用助理"}'
    await inbound.accept(stop)
    stop_task = await claimed(db_session, "chat")
    assert (await turn(db_engine, stop_task)).requests == []
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "send")
    )
    assert card is not None
    click_task = await stop_click(db_session, bot, card, who=OWNER_PID, chat=CHAT)
    await CardActionHandler().run(build_ctx(db_engine, click_task))
    for _ in range(6):
        await tick(factory, messenger, later=30)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.action == "deprovision" and flow.outcome == "done"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.token_enc is None and auth.revoked
    assert ("workspace-remove", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}) in (
        node.calls
    )

    # ---- 库里留下的内容：仍是密文，再也解不开 --------------------------------------------------
    keys.forget()
    sealed.forget()
    stored = str(await raw(db_session, "SELECT args_enc::text FROM assistant_approvals"))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    payload = str(
        await raw(
            db_session, "SELECT payload::text FROM inbound_events WHERE id = :id", id=event.id
        )
    )
    assert "你好" not in payload
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, Cipher(MASTER), owner.id)
    locked = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert locked is not None and sealed.is_locked(locked) and locked.args_enc is None
    assert await db_session.scalar(text("SELECT count(*) FROM assistant_lifecycles")) == 2
