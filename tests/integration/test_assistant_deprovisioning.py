"""停用：本人在私聊里确认 → 停下轮次 → 撤回待批 → 撤销授权 → 销毁密钥、停用记录 → 删工作目录。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import deprovisioning, keys, runtime
from coreman.core.bus import tasks
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    AssistantRetiredDir,
    Bot,
    FeishuPersonalGrant,
    Task,
)
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import service as personal
from tests.integration.assistant_helpers import seed_standard
from tests.integration.lifecycle_helpers import (
    CIPHER,
    FakeMessenger,
    active_assistant,
    flow_of,
    install,
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
