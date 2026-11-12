"""停用：本人在私聊里确认 → 停下轮次 → 撤回待批 → 撤销授权 → 销毁密钥、停用记录 → 删工作目录。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import approvals, deprovisioning, keys, lifecycle_runner, runtime
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    AssistantApproval,
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    FeishuPersonalGrant,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import seed_standard
from tests.integration.lifecycle_helpers import (
    APP_ID,
    CIPHER,
    TENANT,
    FakeMessenger,
    active_assistant,
    flow_of,
    install,
    member,
    tick,
)
from tests.integration.test_chat_handler import chat_task

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def test_deprovisioning_leaves_nothing_usable(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, auth = install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    flow = await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    assert flow is not None and flow.step == "drain"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning" and bot.enabled
    assert await runtime.turn_blocker(db_session, bot) == "deprovisioning"
    # 重复请求不重复开流程。
    assert (await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)) == flow
    await db_session.commit()
    messenger = FakeMessenger()
    factory = make_session_factory(db_engine)
    for _ in range(6):
        await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned" and not bot.enabled
    assert bot.bot_key.endswith(f"-x{bot.id.hex[:8]}")
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.status == "revoked" and grant.token_enc is None
    assert auth.revoked and auth.revoked[0]["refresh_token"] == "u-refresh"
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.dek_enc is None and key.destroyed_at is not None
    keys.forget()
    with pytest.raises(keys.KeyDestroyed):
        await keys.data_cipher(db_session, CIPHER, owner.id)
    assert ("workspace-remove", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}) in (
        node.calls
    )
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    assert messenger.texts[-1] == ("owner_pid", deprovisioning.DONE_TEXT["owner"])
    assert await deprovisioning.request(db_session, bot, trigger="owner", actor_id=None) is None


async def test_an_ordinary_bot_cannot_be_sent_into_deprovisioning(db_session: AsyncSession) -> None:
    standard = await seed_standard(db_session)
    with pytest.raises(ValueError):
        await deprovisioning.request(db_session, standard, trigger="admin", actor_id=None)
    assert standard.assistant_state is None


async def test_a_member_who_leaves_gets_a_grace_period(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    owner.status = "disabled"
    await db_session.commit()
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "grace" and flow.trigger == "member_left"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning"
    # 宽限期内账号恢复：作罢，助理照常可用，什么都没销毁。
    owner.status = "active"
    await db_session.commit()
    await tick(factory, messenger, later=deprovisioning.GRACE_CHECK.total_seconds() + 1)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "cancelled"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "active"
    # 再次停用并超过宽限期：照常停用到底，不给已离开的人发消息。
    owner.status = "disabled"
    await db_session.commit()
    await tick(factory, messenger)
    await db_session.execute(
        update(AssistantLifecycle)
        .where(AssistantLifecycle.finished_at.is_(None))
        .values(deadline=func.now() - text("interval '1 second'"), next_attempt_at=func.now())
    )
    await db_session.commit()
    for _ in range(6):
        await tick(factory, messenger, later=1)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done" and flow.trigger == "member_left"
    assert messenger.texts == []
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is not None


async def test_running_turns_are_stopped_before_anything_is_withdrawn(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    queued = await chat_task(
        db_session, bot, "再查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    queued.status = "running"
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "drain"
    row = await db_session.get(Task, queued.id, populate_existing=True)
    assert row is not None and row.cancel_requested_at is not None
    await tasks.finish(db_session, queued.id, status="cancelled")
    await db_session.commit()
    await tick(factory, messenger, later=11)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "withdraw"


async def test_a_failing_step_is_retried_from_where_it_stopped(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    real = personal.revoke_grant

    async def broken(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(personal, "revoke_grant", broken)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for hour in range(1, 5):
        await tick(factory, messenger, later=3600 * hour)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "revoke" and flow.attempts >= 2
    assert flow.error_code == "RuntimeError" and flow.finished_at is None
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is None
    monkeypatch.setattr(personal, "revoke_grant", real)
    for hour in range(5, 8):
        await tick(factory, messenger, later=3600 * hour)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"


async def test_a_retired_directory_waits_for_its_node(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    node.down = True
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.attempts >= 1 and row.error_code == "AgentError"
    node.down = False
    await tick(factory, messenger, later=86400)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0
    assert node.ops().count("workspace-remove") == 1


async def test_a_directory_the_node_says_is_not_ours_is_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    node.removed = {"removed": False, "existed": True}
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_a_directory_still_used_by_a_turn_is_retried_later(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, _ = install(monkeypatch)
    node.removed = {"removed": False, "existed": True, "busy": True}
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for _ in range(6):
        await tick(factory, messenger)
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.error_code == "busy" and row.attempts == 1
    node.removed = {"removed": True, "existed": True}
    await tick(factory, messenger, later=86400)
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_the_scheduler_runs_lifecycles_in_their_own_loop_and_survives_failures(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看护进程单独一条循环推进开通与停用：每次都出错也不影响与普通员工共用的那一轮看护。"""
    import asyncio

    from coreman.core.assistants import lifecycle_runner
    from coreman.runtime.scheduler import reaper, service
    from coreman.runtime.scheduler.service import SchedulerService

    calls: list[int] = []
    cleaned = asyncio.Event()

    async def broken(*_args: object, **_kw: object) -> dict[str, int]:
        calls.append(1)
        raise RuntimeError("synthetic")

    async def cleanup(*_args: object, **_kw: object) -> dict[str, int]:
        cleaned.set()
        return {}

    monkeypatch.setattr(lifecycle_runner, "tick", broken)
    monkeypatch.setattr(reaper, "run_cleanup", cleanup)
    monkeypatch.setattr(service, "ASSISTANT_LIFECYCLE_SECONDS", 0.05)
    svc = SchedulerService(port=0, tick_seconds=0.2, cleanup_seconds=0.5, leader_retry_seconds=0.2)
    task = asyncio.create_task(svc.run())
    try:
        await asyncio.wait_for(cleaned.wait(), 10)
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        svc.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(calls) >= 2


# ---- 评审补充：宽限期内的停用、等在跑的任务、销毁之前先撤回 ------------------------------------


def _approval(bot_id: Any, status: str = "pending") -> AssistantApproval:
    return AssistantApproval(
        bot_id=bot_id,
        tool="feishu_reply_message",
        args_enc={"calls": [], "abstract": "私聊 → 李四"},
        args_hash=uuid.uuid4().hex,
        request_hash="r",
        channel="p2p",
        topic="报价",
        status=status,
        external_id=f"cm-pa-{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(hours=72),
    )


async def _execution(session: AsyncSession, bot: Bot) -> Task:
    """一张已批准、排着队等执行的写操作。"""
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=APPROVAL_EXECUTE_KIND,
            user_id=bot.owner_id,
            dedupe_key=f"assistant-approval:{uuid.uuid4()}:execute",
            payload={"approval_id": str(uuid.uuid4())},
        ),
    )
    assert task is not None
    return task


async def _step(session: AsyncSession, owner_id: Any) -> str | None:
    flow = await flow_of(session, owner_id)
    assert flow is not None
    return flow.step if flow.finished_at is None else flow.outcome


async def test_an_explicit_stop_during_grace_is_not_undone_when_the_account_returns(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    owner.status = "disabled"
    await db_session.commit()
    await tick(factory, messenger)
    assert await _step(db_session, owner.id) == "grace"
    admin = await member(db_session, login="lisi", pid="lisi_pid")
    flow = await deprovisioning.request(db_session, bot, trigger="admin", actor_id=admin.id)
    await db_session.commit()
    assert flow is not None and flow.step == "drain" and flow.trigger == "admin"
    assert flow.actor_id == admin.id and flow.deadline is None
    owner.status = "active"
    await db_session.commit()
    for _ in range(6):
        await tick(factory, messenger, later=1)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.outcome == "done"
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioned"


async def test_after_grace_drain_still_waits_for_running_work(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    monkeypatch.setattr(deprovisioning, "GRACE", timedelta(0))
    bot, owner, _ = await active_assistant(db_session)
    running = await chat_task(
        db_session, bot, "再查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    running.status = "running"
    owner.status = "disabled"
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)
    assert await _step(db_session, owner.id) == "grace"
    # 宽限期到了：经 step_grace 自己进入 drain。
    await tick(factory, messenger, later=deprovisioning.GRACE_CHECK.total_seconds() + 1)
    assert await _step(db_session, owner.id) == "drain"
    await tick(factory, messenger, later=deprovisioning.GRACE_CHECK.total_seconds() + 2)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "drain" and flow.deadline is not None
    assert flow.deadline > utcnow() + timedelta(minutes=14)
    row = await db_session.get(Task, running.id, populate_existing=True)
    assert row is not None and row.cancel_requested_at is not None


async def test_drain_waits_for_a_queued_approved_execution_without_cancelling_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    other = await seed_standard(db_session)
    theirs = await chat_task(db_session, other, "别的员工在忙", sender="lisi")
    theirs.status = "running"
    execution = await _execution(db_session, bot)
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)
    await tick(factory, messenger, later=11)
    assert await _step(db_session, owner.id) == "drain"
    row = await db_session.get(Task, execution.id, populate_existing=True)
    assert row is not None and row.status == "queued" and row.cancel_requested_at is None
    row = await db_session.get(Task, theirs.id, populate_existing=True)
    assert row is not None and row.status == "running" and row.cancel_requested_at is None
    await tasks.finish(db_session, execution.id, status="succeeded")
    await db_session.commit()
    await tick(factory, messenger, later=22)
    assert await _step(db_session, owner.id) == "withdraw"


async def test_drain_gives_up_waiting_after_fifteen_minutes(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    stuck = await chat_task(
        db_session, bot, "再查一下", sender="owner_pid", chat_type="single", chat_id="oc_private"
    )
    stuck.status = "running"
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)
    await tick(factory, messenger, later=11)
    assert await _step(db_session, owner.id) == "drain"
    shift = deprovisioning.DRAIN_WAIT + timedelta(minutes=1)
    real = deprovisioning.utcnow
    monkeypatch.setattr(deprovisioning, "utcnow", lambda: real() + shift)
    await tick(factory, messenger, later=shift.total_seconds())
    assert await _step(db_session, owner.id) == "withdraw"


async def test_pending_approvals_are_withdrawn_before_the_key_is_destroyed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    early = _approval(bot.id)
    db_session.add(early)
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await tick(factory, messenger)  # drain → withdraw
    await tick(factory, messenger)  # withdraw → revoke
    assert await _step(db_session, owner.id) == "revoke"
    # 撤回这一步已经提交，密钥还在。
    row = await db_session.get(AssistantApproval, early.id, populate_existing=True)
    assert row is not None and row.status == "withdrawn"
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is None
    # 撤回之后又冒出来一张：销毁那一步再撤一次。
    late = _approval(bot.id)
    db_session.add(late)
    await db_session.commit()
    for _ in range(3):
        await tick(factory, messenger)
    assert await _step(db_session, owner.id) == "done"
    row = await db_session.get(AssistantApproval, late.id, populate_existing=True)
    assert row is not None and row.status == "withdrawn"
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is not None


async def test_a_failing_withdraw_keeps_the_key_and_the_record_deprovisioning(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    db_session.add(_approval(bot.id))
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()

    async def broken(*_args: Any, **_kw: Any) -> int:
        raise RuntimeError("approval system hiccup")

    monkeypatch.setattr(approvals, "withdraw_pending", broken)
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    for hour in range(0, 5):
        await tick(factory, messenger, later=3600 * hour)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "withdraw" and flow.finished_at is None
    assert flow.error_code == "RuntimeError"
    key = await db_session.get(AssistantKey, owner.id, populate_existing=True)
    assert key is not None and key.destroyed_at is None and key.dek_enc is not None
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None and bot.assistant_state == "deprovisioning"


async def test_a_flow_the_runner_cannot_run_is_pushed_back_and_does_not_block_others(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    # 没有处理函数的步骤（_run_one 在 try 之外就出错）。
    monkeypatch.delitem(deprovisioning.STEPS, "drain")
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    now = utcnow()
    await lifecycle_runner.tick(factory, CIPHER, now, messenger=messenger)
    flow = await flow_of(db_session, owner.id)
    assert flow is not None and flow.step == "drain" and flow.attempts == 1
    assert flow.error_code == "KeyError"
    assert flow.next_attempt_at is not None and flow.next_attempt_at > now


async def test_request_takes_the_flow_before_the_record_like_the_runner(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看护进程先锁流程行再锁记录；请求也必须这个顺序，否则两边相遇就死锁。"""
    import asyncio

    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="admin", actor_id=None)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    async with factory() as runner, factory() as caller, factory() as probe:
        held = await runner.scalar(
            select(AssistantLifecycle)
            .where(AssistantLifecycle.owner_id == owner.id)
            .with_for_update()
        )
        assert held is not None
        held_id = held.id
        theirs = await caller.get(Bot, bot.id)
        assert theirs is not None
        waiting = asyncio.create_task(
            deprovisioning.request(caller, theirs, trigger="owner", actor_id=owner.id)
        )
        try:
            await asyncio.sleep(0.3)
            assert not waiting.done()
            # 请求在等流程行，记录还没被它锁住。
            await probe.execute(text("SET LOCAL lock_timeout = '200ms'"))
            locked = await probe.scalar(select(Bot.id).where(Bot.id == bot.id).with_for_update())
            assert locked == bot.id
        finally:
            await probe.rollback()
            await runner.rollback()
        flow = await asyncio.wait_for(waiting, 5)
        flow_id = flow.id if flow is not None else None
        await caller.commit()
    assert flow_id == held_id


async def test_concurrent_requests_open_one_flow(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    factory = make_session_factory(db_engine)

    async def one(trigger: str) -> Any:
        async with factory() as session:
            mine = await session.get(Bot, bot.id)
            assert mine is not None
            flow = await deprovisioning.request(session, mine, trigger=trigger, actor_id=None)
            await session.commit()
            return flow.id if flow is not None else None

    ids = await asyncio.gather(one("admin"), one("owner"), one("admin"))
    assert len(set(ids)) == 1 and ids[0] is not None
    count = await db_session.scalar(
        select(func.count())
        .select_from(AssistantLifecycle)
        .where(AssistantLifecycle.owner_id == owner.id, AssistantLifecycle.action == "deprovision")
    )
    assert count == 1


async def _until(
    factory: Any, messenger: FakeMessenger, session: AsyncSession, owner_id: Any, step: str
) -> None:
    for _ in range(6):
        if await _step(session, owner_id) == step:
            return
        await tick(factory, messenger, later=1)
    assert await _step(session, owner_id) == step


def _owner_scope(bot: Bot, owner_id: Any, task: Task) -> Any:
    from coreman.core.feishu_personal.policy import Scope

    return Scope(task, bot, owner_id, TENANT, APP_ID, "ou_owner", "owner_pid", "oc_private")


async def test_no_new_authorisation_once_the_grant_was_revoked(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤销授权之后、销毁之前：旧的授权卡片、续接查询都发不起、也完不成新的授权。"""
    _, auth = install(monkeypatch)
    bot, owner, task = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await _until(factory, messenger, db_session, owner.id, "destroy")
    bot = await db_session.get(Bot, bot.id, populate_existing=True)
    assert bot is not None
    scope = _owner_scope(bot, owner.id, task)
    for start in (
        lambda: personal.begin_in_flow(db_session, CIPHER, scope, "all"),
        lambda: personal.authorization_status(db_session, CIPHER, scope),
    ):
        with pytest.raises(personal.PersonalError) as caught:
            await start()
        assert caught.value.code == "assistant_inactive"
    await db_session.rollback()
    assert auth.issued == 0


async def test_a_pending_grant_left_at_destroy_is_cleared(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤销之后又冒出一个进行中的授权（比如撤销前已发出的链接）：销毁那一步再撤一次，什么都不留。"""
    _, auth = install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    await deprovisioning.request(db_session, bot, trigger="owner", actor_id=owner.id)
    await db_session.commit()
    factory, messenger = make_session_factory(db_engine), FakeMessenger()
    await _until(factory, messenger, db_session, owner.id, "destroy")
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None
    grant.status, grant.pending_enc = "pending", "enc:pending"
    grant.pending_expires_at = utcnow() + timedelta(minutes=10)
    await db_session.commit()
    await _until(factory, messenger, db_session, owner.id, "done")
    grant = await db_session.get(FeishuPersonalGrant, (bot.id, owner.id), populate_existing=True)
    assert grant is not None and grant.status == "revoked"
    assert grant.pending_enc is None and grant.token_enc is None
    assert grant.pending_expires_at is None


async def test_stopping_an_old_record_leaves_the_current_one_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理员停用一条早已停用完的旧记录：本人现在那条记录的宽限期不受影响。"""
    from tests.integration.assistant_helpers import into_assistant

    install(monkeypatch)
    bot, owner, _ = await active_assistant(db_session)
    old = await seed_standard(db_session)
    old.enabled = False
    await into_assistant(db_session, old, owner)
    old.assistant_state, old.bot_key = "deprovisioned", "assistant-old-x1"
    await db_session.commit()
    flow = await deprovisioning.request(db_session, bot, trigger="member_left", actor_id=None)
    await db_session.commit()
    assert flow is not None and flow.step == "grace"
    assert (await deprovisioning.request(db_session, old, trigger="admin", actor_id=None)) is None
    await db_session.commit()
    current = await flow_of(db_session, owner.id)
    assert current is not None and current.id == flow.id
    assert current.step == "grace" and current.trigger == "member_left"
    # 另一条还没停用完的旧记录（状态为空）也不去动本人正在停用的那条流程。
    old.assistant_state = None
    await db_session.commit()
    assert (await deprovisioning.request(db_session, old, trigger="admin", actor_id=None)) is None
    await db_session.commit()
    current = await flow_of(db_session, owner.id)
    assert current is not None and current.step == "grace" and current.trigger == "member_left"
