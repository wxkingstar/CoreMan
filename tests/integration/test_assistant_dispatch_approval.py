"""要对方改东西的调度先过审批：未批准前不派出，批准后派出且只派一次，被拒、失败投回。"""

from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import dispatch, platform_turns
from coreman.core.assistants.approvals import dispatching, execute, service, tower
from coreman.core.assistants.kinds import (
    APPROVAL_EXECUTE_KIND,
    APPROVAL_RESULT_KIND,
    BOT_HELP_KIND,
    HANDOVER_KIND,
)
from coreman.core.db.models import (
    AssistantApproval,
    AssistantJob,
    AuditLog,
    Bot,
    InboundEvent,
    User,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from coreman.runtime.worker.assistant_approvals import AssistantApprovalExecuteHandler
from tests.integration.approval_helpers import CFG, FakeTower, decided, install_tower, queued
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.dispatch_helpers import QUESTION, rows_in, target_bot
from tests.integration.job_helpers import take, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")
DISPATCH_CFG = tower.Config(
    "https://tower.example.com",
    "sender-key",
    "dispatch-key",
    "https://coreman.example.com",
    tower.DISPATCH_TARGET,
)
STORED = "SELECT args_enc::text FROM assistant_approvals WHERE id = :id"


async def owner_scope(
    session: AsyncSession, *, unattended: bool = False
) -> tuple[Bot, User, Scope]:
    """所有者私聊助理的一轮（或一轮无人在场的交回）核验后的身份。"""
    bot, owner = await assistant(session)
    if unattended:
        task = await platform_turns.enqueue(
            session, bot, HANDOVER_KIND, {}, dedupe_key="handover:1"
        )
        assert task is not None
        event = None
    else:
        task = await chat_task(
            session,
            bot,
            "让库存助手改一下",
            sender="owner_pid",
            chat_type="single",
            chat_id="oc_private",
        )
        event = await session.get(InboundEvent, task.inbound_event_id)
    await session.commit()
    scope = Scope(
        task,
        bot,
        owner.id,
        "tenant-example",
        "cli_example",
        "ou_owner",
        "owner_pid",
        "oc_private",
        event,
        unattended,
    )
    return bot, owner, scope


async def submitted(
    session: AsyncSession, scope: Scope, target: Bot, question: str = QUESTION
) -> AssistantApproval:
    out = await dispatching.submit(
        session, scope, target=target, question=question, cfg=DISPATCH_CFG
    )
    await session.commit()
    assert out["status"] == "pending_approval", out
    row = await session.get(AssistantApproval, uuid.UUID(out["approval_id"]))
    assert row is not None
    return row


async def approved(engine: AsyncEngine, session: AsyncSession, row: AssistantApproval) -> str:
    """所有者批准（回调排下执行任务），执行一遍；返回审批单的状态。"""
    await decided(session, row)
    assert len(await queued(session, APPROVAL_EXECUTE_KIND)) == 1
    return await dispatching.run(make_session_factory(engine), row.id, DISPATCH_CFG)


async def _dispatched(session: AsyncSession, target: Bot) -> int:
    return len(await tasks_of(session, target, BOT_HELP_KIND))


async def test_nothing_goes_out_before_the_owner_approves(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    bot, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session, name="库存\n助手")
    row = await submitted(db_session, scope, target)
    assert (row.tool, row.channel, row.status) == ("assistant_ask_bot", "other", "pending")
    assert await _dispatched(db_session, target) == 0
    assert await rows_in(db_session, "assistant_jobs") == 0
    assert await rows_in(db_session, "audit_logs") == 0
    # 审批单上是目标员工与问题原文；摘要只有渠道和对象，名字里的换行压成一行。
    (sent,) = fake.submitted
    assert sent["abstract"] == "调度员工 → 库存 助手" and sent["requester"] == "owner"
    assert sent["caller"] == bot.bot_key
    assert "对象：库存 助手（demo-bot）" in sent["content"]
    assert f"│ {QUESTION}" in sent["content"] and "动作：" in sent["content"]
    # 库里的参数是密文；哈希绑的是调度的目标系统。
    assert SECRET not in str(await raw(db_session, STORED, id=row.id))
    assert row.request_hash == tower.request_hash(
        DISPATCH_CFG, row.external_id, sent["body"], bot.bot_key
    )
    assert row.request_hash != tower.request_hash(CFG, row.external_id, sent["body"], bot.bot_key)
    # 同一个问题再提交一次：还是那一张。
    again = await dispatching.submit(
        db_session, scope, target=target, question=QUESTION, cfg=DISPATCH_CFG
    )
    assert again == {"status": "pending_approval", "approval_id": str(row.id), "duplicate": True}
    assert len(fake.submitted) == 1


async def test_an_approved_dispatch_goes_out_once(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    bot, owner, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    assert await approved(db_engine, db_session, row) == "executed"
    # 行的编号就是审批单的编号；目标那一轮带着「批准过可以改」。
    job = await db_session.get(AssistantJob, row.id, populate_existing=True)
    assert job is not None and (job.kind, job.target_bot_id) == ("bot_help", target.id)
    assert job.brief_enc == QUESTION
    (round_,) = await tasks_of(db_session, target, BOT_HELP_KIND)
    assert round_.payload["allow_write"] is True and round_.id == job.last_task_id
    assert fake.redeemed and fake.reported[-1] == (row.approval_id, "executed", None)
    audit = (await db_session.scalars(select(AuditLog))).one()
    assert audit.action == dispatch.AUDIT_ACTION and audit.actor_id == owner.id
    assert (audit.diff["allow_write"], audit.diff["approved"], audit.diff["unattended"]) == (
        True,
        True,
        False,
    )
    # 批准并派出之后不单独投一轮：答复由结果回合带回。
    assert await tasks_of(db_session, bot, APPROVAL_RESULT_KIND) == []
    # 执行任务重跑：不派第二次。
    again = await dispatching.run(make_session_factory(db_engine), row.id, DISPATCH_CFG)
    assert again == "executed"
    assert await _dispatched(db_session, target) == 1
    assert await rows_in(db_session, "assistant_jobs") == 1


async def test_a_rejected_dispatch_is_reported_and_never_sent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_tower(monkeypatch)
    bot, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    await decided(db_session, row, "rejected", "先别动数据")
    assert await _dispatched(db_session, target) == 0
    assert await rows_in(db_session, "assistant_jobs") == 0
    (result,) = await tasks_of(db_session, bot, APPROVAL_RESULT_KIND)
    assert result.session_key == "oc_private"
    found = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None and found.status == "rejected"
    text = service.result_text(found)
    assert "assistant_ask_bot" in text and "调度员工 → demo-bot" in text and "被拒绝" in text


async def test_an_unattended_request_is_audited_as_such_once_approved(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_tower(monkeypatch)
    _, _, scope = await owner_scope(db_session, unattended=True)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    assert await approved(db_engine, db_session, row) == "executed"
    audit = (await db_session.scalars(select(AuditLog))).one()
    assert (audit.diff["unattended"], audit.diff["approved"]) == (True, True)


async def test_losing_access_while_waiting_fails_the_approval(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    bot, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    target.accept_bot_help = False
    await db_session.commit()
    assert await approved(db_engine, db_session, row) == "failed"
    found = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None and found.error_code == "bot_not_available"
    assert await _dispatched(db_session, target) == 0
    assert fake.reported[-1] == (row.approval_id, "failed", "bot_not_available")
    assert len(await tasks_of(db_session, bot, APPROVAL_RESULT_KIND)) == 1


async def test_an_outage_changes_nothing_and_a_tampered_request_is_refused(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    _, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    await decided(db_session, row)
    factory = make_session_factory(db_engine)
    fake.down = True
    with pytest.raises(execute.Retry):
        await dispatching.run(factory, row.id, DISPATCH_CFG)
    waiting = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert waiting is not None and waiting.status == "approved"
    assert waiting.execute_started_at is None and await _dispatched(db_session, target) == 0
    fake.down = False
    # 库里的问题被换掉：核销时哈希对不上，不派出。
    # 深拷贝后再改：原地改嵌套的值，ORM 看不出这一列变了。
    stored = copy.deepcopy(dict(waiting.args_enc or {}))
    stored["calls"][0]["arguments"]["question"] = "把库存全部清零"
    waiting.args_enc = stored
    await db_session.commit()
    assert await dispatching.run(factory, row.id, DISPATCH_CFG) == "failed"
    refused = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert refused is not None and refused.error_code == "request_mismatch"
    assert await _dispatched(db_session, target) == 0


async def test_without_the_target_system_the_request_fails_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_tower(monkeypatch)
    _, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    out = await dispatching.submit(db_session, scope, target=target, question=QUESTION, cfg=None)
    assert out["error"] == service.UNAVAILABLE and fake.submitted == []
    assert await rows_in(db_session, "assistant_approvals") == 0


@pytest.mark.usefixtures("runtime_settings")
async def test_the_worker_routes_dispatch_approvals_to_their_own_target(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeTower = install_tower(monkeypatch)
    _, _, scope = await owner_scope(db_session)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    await decided(db_session, row)
    monkeypatch.setattr(
        tower,
        "config",
        lambda _settings, target=tower.TARGET: (
            DISPATCH_CFG if target == tower.DISPATCH_TARGET else CFG
        ),
    )
    queued_task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[-1]
    claimed = await take(db_session, queued_task.id)
    await AssistantApprovalExecuteHandler().run(build_ctx(db_engine, claimed))
    done = await db_session.get(AssistantApproval, row.id, populate_existing=True)
    assert done is not None and done.status == "executed"
    assert await _dispatched(db_session, target) == 1
    # 核销用的是调度目标系统算出的哈希。
    assert fake.redeemed[-1][1] == row.request_hash


async def test_an_unattended_request_is_refused_when_the_runtime_turned_private(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.db.models import RelayServer

    install_tower(monkeypatch)
    _, _, scope = await owner_scope(db_session, unattended=True)
    target = await target_bot(db_session)
    row = await submitted(db_session, scope, target)
    # 提交之后目标员工的运行时改成了别人的私有运行时：批准了也不派出。
    relay = await db_session.get(RelayServer, target.relay_server_id)
    assert relay is not None
    relay.visibility, relay.owner_id = "private", target.created_by
    await db_session.commit()
    assert await approved(db_engine, db_session, row) == "failed"
    await db_session.refresh(row)
    assert row.error_code == "owner_turn_required"
    assert await _dispatched(db_session, target) == 0
