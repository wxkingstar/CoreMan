"""个人助理用哪台运行时：池里挑最空的节点；只派到能隔离运行的节点；不行就暂停并告诉所有者。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import runtime
from coreman.core.bots.switch_relay import SwitchError, validate_switch
from coreman.core.db.models import (
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import now
from tests.fakes.runtime_node import add_node, add_node_relay
from tests.integration.assistant_helpers import seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")


def ready_caps(provider: str = "claude") -> dict:
    return {provider: {"installed": True, "login": "ready", runtime.SANDBOX_CAPABILITY: True}}


async def pool_relay(session: AsyncSession, name: str, **node_values) -> RelayServer:
    node_values.setdefault("capabilities", ready_caps())
    node_values.setdefault("heartbeat_at", now())
    node = await add_node(session, name=name, **node_values)
    return await add_node_relay(session, node=node, assistant_pool=True)


def test_reasons_cover_every_way_a_node_can_fail() -> None:
    relay = RelayServer(
        runtime_node_id=uuid.uuid4(), name="r", model_provider="claude", is_active=True
    )
    base = {"id": relay.runtime_node_id, "is_active": True, "draining": False}
    node = RuntimeNode(**base, heartbeat_at=now(), capabilities=ready_caps())
    assert runtime.unavailable_reason(relay, node) is None
    assert runtime.unavailable_reason(relay, None) == "runtime_disabled"
    stale = RuntimeNode(
        **base, heartbeat_at=now() - timedelta(minutes=5), capabilities=ready_caps()
    )
    assert runtime.unavailable_reason(relay, stale) == "runtime_offline"
    draining = RuntimeNode(
        **{**base, "draining": True}, heartbeat_at=now(), capabilities=ready_caps()
    )
    assert runtime.unavailable_reason(relay, draining) == "runtime_draining"
    old = RuntimeNode(**base, heartbeat_at=now(), capabilities={"claude": {"login": "ready"}})
    assert runtime.unavailable_reason(relay, old) == "sandbox_unsupported"
    other = RuntimeNode(**base, heartbeat_at=now(), capabilities=ready_caps("codex"))
    assert runtime.unavailable_reason(relay, other) == "sandbox_unsupported"
    logged_out = RuntimeNode(
        **base,
        heartbeat_at=now(),
        capabilities={"claude": {"login": "required", runtime.SANDBOX_CAPABILITY: True}},
    )
    assert runtime.unavailable_reason(relay, logged_out) == "login_required"
    for reason in runtime.PAUSE_MESSAGES:
        assert runtime.pause_message(reason).startswith("你的个人助理暂停了：")


def test_eligible_relays_are_the_pool_or_the_owners_private_runtime() -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    assert runtime.eligible(RelayServer(assistant_pool=True, visibility="all"), me)
    assert runtime.eligible(
        RelayServer(assistant_pool=False, visibility="private", owner_id=me), me
    )
    assert not runtime.eligible(
        RelayServer(assistant_pool=False, visibility="private", owner_id=other), me
    )
    assert not runtime.eligible(RelayServer(assistant_pool=False, visibility="all"), me)


async def test_pick_pool_relay_takes_the_emptiest_ready_node(db_session: AsyncSession) -> None:
    busy = await pool_relay(db_session, "busy")
    idle = await pool_relay(db_session, "idle")
    await pool_relay(db_session, "offline", heartbeat_at=now() - timedelta(minutes=5))
    await pool_relay(
        db_session, "old", capabilities={"claude": {"installed": True, "login": "ready"}}
    )
    outside = await add_node_relay(db_session, name="outside")
    outside_node = await db_session.get(RuntimeNode, outside.runtime_node_id)
    assert outside_node is not None
    outside_node.capabilities, outside_node.heartbeat_at = ready_caps(), now()
    for _ in range(2):
        bot, _ = await seed_assistant(db_session)
        bot.relay_server_id = busy.id
    # 节点上的普通员工不算数。
    standard = await seed_standard(db_session)
    standard.relay_server_id = idle.id
    await db_session.commit()
    assert (await runtime.pick_pool_relay(db_session, "claude")).id == idle.id  # type: ignore[union-attr]
    mine, _ = await seed_assistant(db_session)
    mine.relay_server_id = idle.id
    other, _ = await seed_assistant(db_session)
    other.relay_server_id = idle.id
    third, _ = await seed_assistant(db_session)
    third.relay_server_id = idle.id
    await db_session.commit()
    assert (await runtime.pick_pool_relay(db_session, "claude")).id == busy.id  # type: ignore[union-attr]
    assert await runtime.pick_pool_relay(db_session, "codex") is None


async def test_turn_blocker_and_assignment(db_session: AsyncSession) -> None:
    bot, owner_user = await seed_assistant(db_session)
    assert await runtime.turn_blocker(db_session, bot) == "runtime_missing"
    shared = await add_node_relay(db_session, name="shared")
    bot.relay_server_id = shared.id
    assert await runtime.turn_blocker(db_session, bot) == "runtime_not_allowed"
    private = await pool_relay(db_session, "private")
    private.assistant_pool, private.visibility, private.owner_id = False, "private", owner_user.id
    bot.relay_server_id = private.id
    assert await runtime.turn_blocker(db_session, bot) is None
    node = await db_session.get(RuntimeNode, private.runtime_node_id)
    assert node is not None
    node.heartbeat_at = now() - timedelta(minutes=5)
    assert await runtime.turn_blocker(db_session, bot) == "runtime_offline"
    assert runtime.assignment_error(shared, None, owner_user.id) is not None
    assert (
        runtime.assignment_error(private, node, owner_user.id)
        == "运行时现在离线，恢复在线后再发一次消息即可"
    )


async def test_switching_an_assistant_only_to_a_sandboxed_eligible_runtime(
    db_session: AsyncSession,
) -> None:
    bot, owner_user = await seed_assistant(db_session)
    home = await pool_relay(db_session, "home")
    bot.relay_server_id = home.id
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    db_session.add(admin)
    await db_session.commit()
    plain = await add_node_relay(db_session, name="plain")
    plain_node = await db_session.get(RuntimeNode, plain.runtime_node_id)
    assert plain_node is not None
    plain_node.heartbeat_at = now()
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=bot, target=plain, model=None, actor=admin, member_ids=set()
        )
    assert "助理池" in caught.value.message
    pool = await pool_relay(db_session, "pool")
    assert await validate_switch(
        db_session, bot=bot, target=pool, model=None, actor=admin, member_ids=set()
    )


async def test_standard_bots_cannot_be_bound_or_switched_to_a_pool_node(
    db_session: AsyncSession,
) -> None:
    from coreman.api.errors import ApiError
    from coreman.api.routers.bots import resolve_relay_for_create

    pool = await pool_relay(db_session, "pool")
    pool_node = await db_session.get(RuntimeNode, pool.runtime_node_id)
    # 同一节点上不在池里的另一种 AI 的实例同样不行：守护进程是同一个。
    beside = await add_node_relay(db_session, node=pool_node, provider="codex")
    assert pool_node is not None
    pool_node.capabilities = {**ready_caps(), **ready_caps("codex")}
    plain = await pool_relay(db_session, "plain")
    plain.assistant_pool = False
    standard = await seed_standard(db_session)
    standard.relay_server_id = plain.id
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    db_session.add(admin)
    await db_session.commit()
    for target in (pool, beside):
        with pytest.raises(SwitchError) as caught:
            await validate_switch(
                db_session, bot=standard, target=target, model=None, actor=admin, member_ids=set()
            )
        assert caught.value.message == runtime.POOL_NODE_MESSAGE
        with pytest.raises(ApiError):
            await resolve_relay_for_create(db_session, admin, target.id, None)
    assert await resolve_relay_for_create(db_session, admin, plain.id, None) is plain


async def test_ordinary_bind_and_switch_cost_one_pool_lookup_and_never_fail_without_a_pool(
    db_session: AsyncSession,
) -> None:
    """普通员工的绑定与换运行时：没有任何助理池时照常通过，只多一条查节点池标记的查询。"""
    from sqlalchemy import event

    from coreman.api.routers.bots import resolve_relay_for_create

    one = await add_node_relay(db_session, name="one")
    two = await add_node_relay(db_session, name="two")
    standard = await seed_standard(db_session)
    standard.relay_server_id = one.id
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    db_session.add(admin)
    await db_session.commit()
    statements: list[str] = []

    def record(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
        statements.append(statement)

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await resolve_relay_for_create(db_session, admin, two.id, None) is two
        bind_lookups = [s for s in statements if "assistant_pool" in s]
        statements.clear()
        await validate_switch(
            db_session, bot=standard, target=two, model=None, actor=admin, member_ids=set()
        )
        switch_lookups = [s for s in statements if "assistant_pool" in s]
        statements.clear()
        # 换到当前这台（同实例换模型）一条都不多。
        await validate_switch(
            db_session, bot=standard, target=one, model=None, actor=admin, member_ids=set()
        )
        same_lookups = [s for s in statements if "assistant_pool" in s]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert (len(bind_lookups), len(switch_lookups), len(same_lookups)) == (1, 1, 0)
