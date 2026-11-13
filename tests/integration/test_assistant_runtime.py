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
    # 助理专用实例都上报本机指纹与系统用户（不是 root）：每轮都按它们核对隔离。
    node_values.setdefault("machine_fingerprint", f"fp-{uuid.uuid4().hex}")
    node_values.setdefault("uid", 1001)
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
    await db_session.commit()
    plain = await add_node_relay(db_session, name="plain")
    plain_node = await db_session.get(RuntimeNode, plain.runtime_node_id)
    assert plain_node is not None
    plain_node.heartbeat_at = now()
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=bot, target=plain, model=None, actor=owner_user, member_ids=set()
        )
    assert "助理池" in caught.value.message
    pool = await pool_relay(db_session, "pool")
    assert await validate_switch(
        db_session, bot=bot, target=pool, model=None, actor=owner_user, member_ids=set()
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
    plain = await pool_relay(
        db_session, "plain", capabilities={"claude": {"installed": True, "login": "ready"}}
    )
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


async def admin_user(session: AsyncSession) -> User:
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    session.add(admin)
    await session.commit()
    return admin


async def record_statements(session: AsyncSession, call) -> list[str]:  # type: ignore[no-untyped-def]
    from sqlalchemy import event

    statements: list[str] = []

    def record(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
        statements.append(statement)

    engine = session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", record)
    try:
        await call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


async def test_ordinary_bind_and_switch_only_add_a_node_lock_and_one_lookup(
    db_session: AsyncSession,
) -> None:
    """普通员工绑定与换运行时：没有助理池时照常通过；多出来的只有节点锁与一条查询，换到当前这台一条不加。"""
    from coreman.api.routers.bots import resolve_relay_for_create

    one = await add_node_relay(db_session, name="one")
    two = await add_node_relay(db_session, name="two")
    three = await add_node_relay(db_session, name="three")
    for relay in (two, three):
        node = await db_session.get(RuntimeNode, relay.runtime_node_id)
        assert node is not None
        if relay is three:
            node.machine_fingerprint, node.uid = "fp-x", 1001
    standard = await seed_standard(db_session)
    standard.relay_server_id = one.id
    admin = await admin_user(db_session)

    async def bind(target: RelayServer) -> None:
        assert await resolve_relay_for_create(db_session, admin, target.id, None) is target

    async def switch(target: RelayServer) -> None:
        await validate_switch(
            db_session, bot=standard, target=target, model=None, actor=admin, member_ids=set()
        )

    def extra(statements: list[str]) -> tuple[int, int, int]:
        lock = sum("pg_advisory_xact_lock" in s for s in statements)
        lookup = sum("assistant_pool" in s for s in statements)
        rest = [
            s for s in statements if "pg_advisory_xact_lock" not in s and "assistant_pool" not in s
        ]
        # 其余语句只可能是原有路径上switch 原有的读取：节点、创建人、模型目录，不能有别的。
        assert all(
            s.startswith(("SELECT runtime_nodes.", "SELECT users.", "SELECT model_catalog."))
            for s in rest
        ), [x[:60] for x in rest]
        return lock, lookup, len(rest)

    # 预热：第一次调用会把目录之类的共用数据读进会话，不属于这次检查。
    for warm in (two, three):
        await bind(warm)
        await switch(warm)
    results = {}
    for label, target in (("plain", two), ("fingerprint", three)):
        results[label, "bind"] = extra(
            await record_statements(db_session, lambda target=target: bind(target))
        )
        results[label, "switch"] = extra(
            await record_statements(db_session, lambda target=target: switch(target))
        )
    results["same"] = extra(await record_statements(db_session, lambda: switch(one)))
    # 锁一条、池标记与助理（含同机同用户兄弟节点）合在一条；节点上报了指纹与 uid 也不多查，
    # 节点能力看已经读进会话的节点，不另查。
    for key in (
        ("plain", "bind"),
        ("plain", "switch"),
        ("fingerprint", "bind"),
        ("fingerprint", "switch"),
    ):
        assert results[key][:2] == (1, 1), key
    assert results["same"][:2] == (0, 0)


async def test_node_sharing_machine_and_uid_with_a_pool_node_takes_no_ordinary_bots(
    db_session: AsyncSession,
) -> None:
    from coreman.api.errors import ApiError
    from coreman.api.routers.bots import resolve_relay_for_create

    pool = await pool_relay(db_session, "pool")
    pool_node = await db_session.get(RuntimeNode, pool.runtime_node_id)
    assert pool_node is not None
    pool_node.machine_fingerprint, pool_node.uid = "fp-1", 1001
    same = await add_node_relay(db_session, name="same")
    other_uid = await add_node_relay(db_session, name="other-uid")
    bare = await add_node_relay(db_session, name="bare")
    for relay, fp, uid in ((same, "fp-1", 1001), (other_uid, "fp-1", 1002), (bare, None, None)):
        node = await db_session.get(RuntimeNode, relay.runtime_node_id)
        assert node is not None
        node.machine_fingerprint, node.uid = fp, uid
    standard = await seed_standard(db_session)
    standard.relay_server_id = bare.id
    admin = await admin_user(db_session)
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=standard, target=same, model=None, actor=admin, member_ids=set()
        )
    assert caught.value.message == runtime.SHARED_POOL_USER_MESSAGE
    with pytest.raises(ApiError):
        await resolve_relay_for_create(db_session, admin, same.id, None)
    assert await resolve_relay_for_create(db_session, admin, other_uid.id, None) is other_uid
    await validate_switch(
        db_session, bot=standard, target=other_uid, model=None, actor=admin, member_ids=set()
    )


async def test_a_disabled_ordinary_employee_still_blocks_pool_admission(
    db_session: AsyncSession,
) -> None:
    relay = await add_node_relay(db_session, name="candidate")
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities, node.machine_fingerprint, node.uid = ready_caps(), "fp-9", 1001
    standard = await seed_standard(db_session)
    standard.relay_server_id = relay.id
    standard.enabled = False
    await db_session.commit()
    assert await runtime.hosts_standard_bots(db_session, node.id)
    assert await runtime.pool_admission_error(db_session, relay, node) is not None
    standard.relay_server_id = None
    await db_session.commit()
    assert await runtime.pool_admission_error(db_session, relay, node) is None


async def test_binding_to_a_node_takes_the_node_lock_first(db_session: AsyncSession) -> None:
    """入池与绑定按节点串行：两条路径都先对目标节点取同一把事务级咨询锁。"""
    from coreman.api.routers.bots import resolve_relay_for_create

    one = await add_node_relay(db_session, name="one")
    two = await add_node_relay(db_session, name="two")
    standard = await seed_standard(db_session)
    standard.relay_server_id = one.id
    admin = await admin_user(db_session)
    keys: list[str] = []

    def spy(statement: str, params: object) -> None:
        if "pg_advisory_xact_lock" in statement:
            keys.append(str(params))

    from sqlalchemy import event

    def record(conn, cursor, statement, parameters, *args):  # type: ignore[no-untyped-def]
        spy(statement, parameters)

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", record)
    try:
        await resolve_relay_for_create(db_session, admin, two.id, None)
        await validate_switch(
            db_session, bot=standard, target=two, model=None, actor=admin, member_ids=set()
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert str(two.runtime_node_id) in keys[0]


async def test_an_ordinary_employee_migrating_to_a_node_keeps_it_out_of_the_pool(
    db_session: AsyncSession,
) -> None:
    """切换隔离里写下的目标也算占用：迁移途中的员工还没绑定，入池不能放过。"""
    old = await add_node_relay(db_session, name="old")
    relay = await add_node_relay(db_session, name="candidate")
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities, node.machine_fingerprint, node.uid = ready_caps(), "fp-7", 1001
    standard = await seed_standard(db_session)
    standard.relay_server_id = old.id
    await db_session.commit()
    assert await runtime.pool_admission_error(db_session, relay, node) is None
    standard.workspace_state, standard.workspace_target_relay_id = "migrating", relay.id
    await db_session.commit()
    assert await runtime.hosts_standard_bots(db_session, node.id)
    assert await runtime.pool_admission_error(db_session, relay, node) is not None
    standard.workspace_state, standard.workspace_target_relay_id = "ready", None
    await db_session.commit()
    assert await runtime.pool_admission_error(db_session, relay, node) is None


async def test_the_node_lock_is_taken_before_the_pool_lookup(db_session: AsyncSession) -> None:
    one = await add_node_relay(db_session, name="one")
    two = await add_node_relay(db_session, name="two")
    await db_session.commit()
    statements = await record_statements(
        db_session, lambda: runtime.standard_bind_error(db_session, two)
    )
    kinds = [
        "lock" if "pg_advisory_xact_lock" in s else "lookup" if "assistant_pool" in s else "other"
        for s in statements
    ]
    assert kinds == ["lock", "lookup"], (one, statements)


# ---- 助理所在节点不承载普通员工（池与私有运行时同一条规则） ---------------------------


async def private_assistant(session: AsyncSession) -> tuple:
    """所有者私有运行时上的助理，节点是专门的助理实例：指纹与 uid 都上报、能隔离运行。"""
    bot, owner_user = await seed_assistant(session)
    private = await pool_relay(session, "private")
    private.assistant_pool, private.visibility, private.owner_id = False, "private", owner_user.id
    bot.relay_server_id = private.id
    await session.commit()
    node = await session.get(RuntimeNode, private.runtime_node_id)
    assert node is not None
    assert await runtime.turn_blocker(session, bot) is None
    return bot, owner_user, private, node


async def test_an_assistant_on_a_node_hosting_an_ordinary_employee_is_paused(
    db_session: AsyncSession,
) -> None:
    bot, _owner, private, node = await private_assistant(db_session)
    beside = await add_node_relay(db_session, node=node, provider="codex")
    standard = await seed_standard(db_session)
    # 同一节点上的另一个实例、同一个实例、停用的、迁移途中的：都算占用。
    for place in ("beside", "same", "disabled", "migrating"):
        standard.relay_server_id = beside.id if place != "same" else private.id
        standard.enabled = place != "disabled"
        if place == "migrating":
            standard.relay_server_id = None
            standard.workspace_state, standard.workspace_target_relay_id = "migrating", beside.id
        await db_session.commit()
        assert await runtime.turn_blocker(db_session, bot) == "runtime_shared", place
    standard.workspace_state, standard.workspace_target_relay_id = "ready", None
    standard.relay_server_id, standard.enabled = None, True
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) is None
    message = runtime.pause_message("runtime_shared")
    assert message.startswith("你的个人助理暂停了：")
    assert str(node.id) not in message and (node.name or "") not in message


async def test_a_sibling_node_with_the_same_machine_and_user_counts_too(
    db_session: AsyncSession,
) -> None:
    bot, _owner, _private, node = await private_assistant(db_session)
    sibling = await add_node_relay(db_session, name="sibling")
    other_user = await add_node_relay(db_session, name="other-user")
    for relay, uid in ((sibling, node.uid), (other_user, 2002)):
        row = await db_session.get(RuntimeNode, relay.runtime_node_id)
        assert row is not None
        row.machine_fingerprint, row.uid = node.machine_fingerprint, uid
    standard = await seed_standard(db_session)
    standard.relay_server_id = other_user.id
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) is None
    standard.relay_server_id = sibling.id
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) == "runtime_shared"


@pytest.mark.parametrize(("fingerprint", "uid"), [(None, 1001), ("fp-z", None), ("fp-z", 0)])
async def test_a_node_without_fingerprint_and_ordinary_uid_cannot_run_assistants(
    db_session: AsyncSession, fingerprint: str | None, uid: int | None
) -> None:
    bot, _owner, _private, node = await private_assistant(db_session)
    node.machine_fingerprint, node.uid = fingerprint, uid
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) == "runtime_shared"


async def test_the_pool_follows_the_same_rule(db_session: AsyncSession) -> None:
    bot, _owner = await seed_assistant(db_session)
    pool = await pool_relay(db_session, "pool")
    bot.relay_server_id = pool.id
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) is None
    standard = await seed_standard(db_session)
    standard.relay_server_id = pool.id
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) == "runtime_shared"


async def test_a_visibility_flip_does_not_unpause_the_assistant(db_session: AsyncSession) -> None:
    """放开成共享、绑上普通员工、再改回私有：助理照样暂停。"""
    from coreman.api.errors import ApiError
    from coreman.api.routers.bots import resolve_relay_for_create

    bot, _owner, private, _node = await private_assistant(db_session)
    private.visibility = "all"
    await db_session.commit()
    admin = await admin_user(db_session)
    with pytest.raises(ApiError):
        await resolve_relay_for_create(db_session, admin, private.id, None)
    # 即便绕过了绑定的检查（比如更早的版本绑上的）：
    standard = await seed_standard(db_session)
    standard.relay_server_id = private.id
    private.visibility = "private"
    await db_session.commit()
    assert await runtime.turn_blocker(db_session, bot) == "runtime_shared"


async def test_switching_an_assistant_to_a_shared_node_is_refused(db_session: AsyncSession) -> None:
    bot, owner_user = await seed_assistant(db_session)
    home = await pool_relay(db_session, "home")
    bot.relay_server_id = home.id
    target = await pool_relay(db_session, "target")
    target.assistant_pool, target.visibility, target.owner_id = False, "private", owner_user.id
    standard = await seed_standard(db_session)
    standard.relay_server_id = target.id
    await db_session.commit()
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=bot, target=target, model=None, actor=owner_user, member_ids=set()
        )
    assert caught.value.message == runtime.PAUSE_MESSAGES["runtime_shared"].removeprefix("绑定的")
    standard.relay_server_id = None
    await db_session.commit()
    await validate_switch(
        db_session, bot=bot, target=target, model=None, actor=owner_user, member_ids=set()
    )


async def test_ordinary_employees_cannot_join_a_node_that_hosts_an_assistant(
    db_session: AsyncSession,
) -> None:
    from coreman.api.errors import ApiError
    from coreman.api.routers.bots import resolve_relay_for_create

    admin = await admin_user(db_session)
    standard = await seed_standard(db_session)
    home = await add_node_relay(db_session, name="home")
    standard.relay_server_id = home.id
    assistant, _owner = await seed_assistant(db_session)
    # 节点没有声明能力、实例也不在池里，只是有个助理绑在上面（或正在迁过来）。
    hosting = await add_node_relay(db_session, name="hosting", visibility="all")
    sibling = await add_node_relay(db_session, name="sibling", visibility="all")
    for relay in (hosting, sibling):
        row = await db_session.get(RuntimeNode, relay.runtime_node_id)
        assert row is not None
        row.machine_fingerprint, row.uid = "fp-shared", 1001
    capable = await add_node_relay(db_session, name="capable", visibility="all")
    capable_node = await db_session.get(RuntimeNode, capable.runtime_node_id)
    assert capable_node is not None
    capable_node.capabilities = {
        **ready_caps("codex"),
        "claude": {"installed": True, "login": "ready"},
    }
    await db_session.commit()

    async def refused(target: RelayServer, message: str) -> None:
        with pytest.raises(SwitchError) as caught:
            await validate_switch(
                db_session, bot=standard, target=target, model=None, actor=admin, member_ids=set()
            )
        assert caught.value.message == message
        with pytest.raises(ApiError):
            await resolve_relay_for_create(db_session, admin, target.id, None)

    await refused(capable, runtime.SANDBOX_NODE_MESSAGE)
    assistant.relay_server_id = hosting.id
    await db_session.commit()
    await refused(hosting, runtime.ASSISTANT_NODE_MESSAGE)
    await refused(sibling, runtime.SHARED_ASSISTANT_USER_MESSAGE)
    assistant.relay_server_id = None
    assistant.workspace_state, assistant.workspace_target_relay_id = "migrating", hosting.id
    await db_session.commit()
    await refused(hosting, runtime.ASSISTANT_NODE_MESSAGE)
    assistant.workspace_state, assistant.workspace_target_relay_id = "ready", None
    await db_session.commit()
    assert await resolve_relay_for_create(db_session, admin, hosting.id, None) is hosting
    await validate_switch(
        db_session, bot=standard, target=sibling, model=None, actor=admin, member_ids=set()
    )


async def test_an_assistant_turn_check_costs_one_isolation_query(db_session: AsyncSession) -> None:
    bot, _owner, _private, _node = await private_assistant(db_session)
    statements = await record_statements(db_session, lambda: runtime.turn_blocker(db_session, bot))
    assert len(statements) == 1 and "FROM bots" in statements[0], statements
